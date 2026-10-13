"""Find shared, evidence-backed campaign situations across a saved sample.

Exact overlaps can nominate a cohort, but cannot prove an offer or a need.
Lexically similar values are only retrieval suggestions: no company is assigned
through them until an explicit equivalence decision is made and revalidated.
"""

from collections import defaultdict
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import combinations
import re

from .campaigns import normalized_value
from .evidence import VERSION as EVIDENCE_VERSION


SPECIFIC_SIGNAL = re.compile(r"^criterion_([a-z0-9_]+)__([0-9a-f]{16})$")
RESEARCH_SCOPE = {"company", "product"}
# These words describe an activity or a generic participant. Alone they do
# not distinguish a campaign audience. A qualifier such as "ПВХ-окна" or
# "производители косметики" still makes the fact diagnostic.
GENERIC_ROOTS = (
    "партнер", "производ", "обслужив", "продаж", "продукт", "услуг",
    "товар", "клиент", "покупател", "заказчик", "компан", "предприят",
    "организац", "бизнес", "сервис", "решен", "предложен", "работ",
    "рынок", "развит", "сотруднич", "издел", "оптов", "рознич",
    "постоянн", "крупн", "дилер", "оборудован", "промышленн",
    "современн",
    "partner", "manufactur", "production", "sale", "service", "product",
    "customer", "client", "company", "business", "solution", "work",
)
STOPWORDS = {"и", "из", "для", "в", "на", "с", "по", "под", "от", "до", "или",
             "the", "and", "of", "for", "to", "in", "a"}
MAX_SUGGESTION_ANCHOR_POSTING = 50
MAX_MODEL_SUGGESTIONS = 20
MIN_SUGGESTION_STRING_SIMILARITY = 0.82


def _content_tokens(label):
    return [token for token in normalized_value(label).split()
            if len(token) >= 3 and token not in STOPWORDS
            and not token.startswith(GENERIC_ROOTS)]


def _diagnostic(label):
    """Require a subject-matter qualifier, not two generic activity words."""
    return bool(_content_tokens(label))


def _specific_fact(signal):
    """Reject generic criterion flags and mismatched value/hash pairs."""
    name = signal.get("name", "")
    match = SPECIFIC_SIGNAL.fullmatch(name)
    if not match or signal.get("state") != "confirmed" or signal.get("confidence") != "HIGH":
        return None
    label = signal.get("label", "").strip()
    value = normalized_value(label)
    if not value or sha256(value.encode()).hexdigest()[:16] != match.group(2):
        return None
    return {"id": name, "criterion_id": match.group(1), "label": label,
            "diagnostic": _diagnostic(label)}


def _anchors(label):
    # A five-character root is a cautious lexical retrieval hint, not a
    # Russian lemmatizer and never proof of semantic equivalence.
    return {token[:5] if len(token) >= 5 else token
            for token in _content_tokens(label)}


def _semantic_suggestions(facts, bindings, *, max_examples):
    postings = defaultdict(set)
    anchors_by_name = {}
    for name, fact in facts.items():
        if not fact["diagnostic"]:
            continue
        anchors = _anchors(fact["label"])
        anchors_by_name[name] = anchors
        for anchor in anchors:
            postings[(fact["criterion_id"], anchor)].add(name)

    pairs = set()
    ignored_anchors = 0
    for names in postings.values():
        if len(names) > MAX_SUGGESTION_ANCHOR_POSTING:
            ignored_anchors += 1
            continue
        pairs.update(combinations(sorted(names), 2))

    suggestions = []
    for left, right in sorted(pairs):
        left_members = set(bindings[left])
        right_members = set(bindings[right])
        # Only show a suggestion if confirming it would actually connect
        # different companies; two variants in one company's profile do not.
        if not left_members - right_members or not right_members - left_members:
            continue
        shared = anchors_by_name[left] & anchors_by_name[right]
        if not shared:
            continue
        score = SequenceMatcher(None, normalized_value(facts[left]["label"]),
                                normalized_value(facts[right]["label"])).ratio()
        if score < MIN_SUGGESTION_STRING_SIMILARITY:
            continue
        suggestions.append({
            "id": "suggestion_" + sha256((left + "\n" + right).encode()).hexdigest()[:16],
            "criterion_id": facts[left]["criterion_id"],
            "left_concept_id": left,
            "right_concept_id": right,
            "left_label": facts[left]["label"],
            "right_label": facts[right]["label"],
            "shared_lexical_anchors": sorted(shared),
            "lexical_similarity": round(score, 3),
            "left_company_count": len(left_members),
            "right_company_count": len(right_members),
            "example_company_ids": sorted(left_members | right_members)[:max_examples],
            "status": "unverified_retrieval_suggestion",
            "requires_review": True,
        })
    suggestions.sort(key=lambda item: (-item["lexical_similarity"],
                                       -(item["left_company_count"] + item["right_company_count"]),
                                       item["id"]))
    return suggestions, ignored_anchors



def _dealer_channel(label):
    """A controlled channel facet, not a generic semantic synonym map."""
    words = normalized_value(label).split()
    if any(word in {"нет", "без", "не"} or word.startswith("отсутств") for word in words):
        return False
    return any(word.startswith("дилер") for word in words)


def _dealer_rule_candidate(facts, bindings, valid_evidence_by_company, *, max_examples, situation=None):
    """Nominate dealer-channel + any concrete product without equating products.

    The result is an unreviewed common-situation rule. An offer and its shared
    message must be checked separately; no company is assigned here.
    """
    channel_names = {name for name, fact in facts.items()
                     if (_matches_situation(fact, situation) if situation else
                         fact["criterion_id"] == "buyer_types" and _dealer_channel(fact["label"]))}
    product_names = {name for name, fact in facts.items()
                     if fact["criterion_id"] == "products" and fact["diagnostic"]}
    by_company = defaultdict(lambda: {"channel": set(), "product": set()})
    for name in channel_names:
        for company_id in bindings[name]:
            by_company[company_id]["channel"].add(name)
    for name in product_names:
        for company_id in bindings[name]:
            by_company[company_id]["product"].add(name)
    members = sorted(company_id for company_id, facets in by_company.items()
                     if facets["channel"] and facets["product"])
    if len(members) < 2:
        return [], {}

    rule_id = "rule_" + situation["id"] if situation else "rule_dealer_channel_any_product"
    member_map = {}
    distinct_citations = []
    independent_sources = []
    for company_id in members:
        facets = by_company[company_id]
        mapped = {}
        for facet in ("channel", "product"):
            names = sorted(facets[facet])
            mapped[facet] = {
                "raw_signal_names": names,
                "evidence_ids": sorted({eid for name in names
                                        for eid in bindings[name][company_id]["evidence_ids"]}),
            }
        member_map[company_id] = mapped
        if len(set(mapped["channel"]["evidence_ids"] + mapped["product"]["evidence_ids"])) >= 2:
            distinct_citations.append(company_id)
        citations = valid_evidence_by_company[company_id]
        if any(a.get("original_source_id") and b.get("original_source_id")
               and a["original_source_id"] != b["original_source_id"]
               and a.get("quote", "").strip().casefold() != b.get("quote", "").strip().casefold()
               for ai in mapped["channel"]["evidence_ids"]
               for bi in mapped["product"]["evidence_ids"]
               for a, b in [(citations[ai], citations[bi])]):
            independent_sources.append(company_id)

    candidate = {
        "id": rule_id,
        "kind": "shared_channel_any_specific_product",
        "channel": "dealer",
        "shared_observation": "В публичных материалах упомянуты дилеры; назван конкретный продукт",
        "claim_limit": "Наличие и масштаб действующей дилерской сети не установлены для каждого участника",
        "required_criteria": ["buyer_types", "products"],
        "company_ids": members,
        "company_count": len(members),
        "example_company_ids": members[:max_examples],
        "two_verified_facts_company_count": len(members),
        "distinct_citation_company_count": len(distinct_citations),
        "independent_source_company_count": len(independent_sources),
        "status": "unreviewed_rule_candidate",
        "requires_offer_review": True,
    }
    if situation:
        candidate.update(kind="shared_situation_any_specific_product", channel=situation["id"],
                         shared_observation=situation["observation"], claim_limit=situation["limit"],
                         required_criteria=[*situation["criteria"], "products"])
    return [candidate], {rule_id: member_map}

# Controlled common situations nominate audiences; they never prove need or an offer.
SITUATIONS = (
    {"id": "hospitality_buyers", "criteria": ["buyer_types"],
     "pattern": r"ресторан|общепит|общественн\w* питан|кафе|horeca|hotel|отел|гостиниц",
     "observation": "Назван конкретный продукт и покупатели из общепита или гостиничного бизнеса",
     "limit": "Не установлены одинаковый формат покупателей, комплексность поставки и потребность в услуге"},
    {"id": "medical_buyers", "criteria": ["buyer_types"],
     "pattern": r"больниц|поликлиник|клиник|медицинск\w* (?:центр|учрежден)",
     "observation": "Назван конкретный продукт и медицинские учреждения среди покупателей",
     "limit": "Не установлены способ закупки, лицо принятия решения и потребность в услуге"},
    {"id": "custom_specification", "criteria": ["application_scenarios"],
     "pattern": r"по (?:вашим |вашим размерам и )?чертеж|по тз заказчик|по размерам заказчик|по чдт|по образцу заказчик",
     "observation": "Назван конкретный продукт и работа по чертежам, размерам или техническому заданию заказчика",
     "limit": "Не установлены доля заказного производства, сложность продажи и потребность в услуге"},
    {"id": "partner_buyers", "criteria": ["buyer_types"],
     "pattern": r"партн[её]р|дистрибьют|интегратор",
     "observation": "Назван конкретный продукт и партнёры, дистрибьюторы или интеграторы среди адресатов",
     "limit": "Эти роли не равнозначны; не установлены действующая сеть, её размер и необходимость развития"},
)


def _matches_situation(fact, situation):
    value = normalized_value(fact["label"])
    if fact["criterion_id"] not in situation["criteria"]:
        return False
    if re.search(r"\b(?:не|нет|без)\b|отсутств", value):
        return False
    return bool(re.search(situation["pattern"], value))


def diverse_candidates(candidates, limit=12):
    """Maximise additional audience coverage; suppress repeated member sets.

    This is a nomination heuristic, never a quality score or membership change.
    Preserve one representative when the audience is entirely covered already.
    """
    remaining=list(candidates);selected=[];covered=set();seen=set()
    while remaining and len(selected)<limit:
        best=max(remaining,key=lambda c:(len(set(c["company_ids"])-covered),
                 c.get("distinct_citation_company_count",0),c["company_count"]))
        remaining.remove(best)
        members=frozenset(best["company_ids"])
        if members in seen:continue
        seen.add(members);covered.update(members);selected.append(best)
    return selected


def build_candidate_overview(profiles, *, max_examples=3):
    """Return global exact cohorts and an auditable candidate→raw-fact map.

    ``profiles`` use the shape passed to ``Pipeline.segment``: company_id,
    signals and validated evidence. ``model_overview`` is compact generation
    input; ``bindings`` retain original names and validated evidence IDs for
    deterministic membership checks against the saved profiles. Similar words
    do not change candidate membership or become evidence.
    """
    if max_examples < 1:
        raise ValueError("max_examples must be positive")

    facts = {}
    company_facts = defaultdict(set)
    bindings = defaultdict(dict)
    valid_evidence_by_company = {}
    company_ids = set()
    for profile in profiles:
        company_id = profile["company_id"]
        if company_id in company_ids:
            raise ValueError("Duplicate company_id in candidate sample")
        company_ids.add(company_id)
        valid_evidence = {
            item["id"]: item for item in profile.get("evidence", [])
            if item.get("validation_version") == EVIDENCE_VERSION
            and item.get("subject_verified") is True
            and item.get("current") is True
            and item.get("scope") in RESEARCH_SCOPE
        }
        valid_evidence_by_company[company_id] = valid_evidence
        for signal in profile.get("signals", []):
            fact = _specific_fact(signal)
            if fact is None:
                continue
            evidence_ids = sorted(set(signal.get("evidence_ids", [])) & valid_evidence.keys())
            if not evidence_ids:
                continue
            concept_id = fact["id"]  # Exact value only; no semantic aliasing.
            facts.setdefault(concept_id, fact)
            company_facts[company_id].add(concept_id)
            previous = bindings[concept_id].get(company_id)
            bindings[concept_id][company_id] = {
                "raw_signal_names": [concept_id],
                "evidence_ids": sorted(set(evidence_ids + (previous or {}).get("evidence_ids", []))),
            }

    # Pair facets rather than complete profiles. This finds shared facts even
    # when every complete company feature vector is unique.
    raw_pair_members = defaultdict(set)
    pair_members = defaultdict(set)
    for company_id, names in company_facts.items():
        for pair in combinations(sorted(names), 2):
            raw_pair_members[pair].add(company_id)
            if all(facts[name]["diagnostic"] for name in pair):
                pair_members[pair].add(company_id)

    candidates = []
    candidate_bindings = {}
    for pair, members in pair_members.items():
        if len(members) < 2:
            continue
        company_list = sorted(members)
        candidate_id = "pair_" + sha256("\n".join(pair).encode()).hexdigest()[:16]
        member_map = {
            company_id: {name: bindings[name][company_id] for name in pair}
            for company_id in company_list
        }
        distinct_citations = []
        independent_sources = []
        for company_id, mapped in member_map.items():
            citations = valid_evidence_by_company[company_id]
            left_items = [citations[eid] for eid in mapped[pair[0]]["evidence_ids"]]
            right_items = [citations[eid] for eid in mapped[pair[1]]["evidence_ids"]]
            if len(set(mapped[pair[0]]["evidence_ids"] + mapped[pair[1]]["evidence_ids"])) >= 2:
                distinct_citations.append(company_id)
            if any(a.get("original_source_id") and b.get("original_source_id")
                   and a["original_source_id"] != b["original_source_id"]
                   and a.get("quote", "").strip().casefold() != b.get("quote", "").strip().casefold()
                   for a in left_items for b in right_items):
                independent_sources.append(company_id)
        candidates.append({
            "id": candidate_id,
            "concept_ids": list(pair),
            "criterion_ids": sorted({facts[name]["criterion_id"] for name in pair}),
            "company_count": len(company_list),
            "company_ids": company_list,
            "example_company_ids": company_list[:max_examples],
            "two_verified_facts_company_count": len(company_list),
            "distinct_citation_company_count": len(distinct_citations),
            "independent_source_company_count": len(independent_sources),
            "independent_source_company_ids": independent_sources,
        })
        candidate_bindings[candidate_id] = member_map

    candidates.sort(key=lambda item: (-item["company_count"],
                                      -item["independent_source_company_count"], item["id"]))
    concepts = [
        {**fact, "company_count": len(bindings[name]),
         "example_company_ids": sorted(bindings[name])[:max_examples]}
        for name, fact in sorted(facts.items())
    ]
    suggestions, ignored_anchors = _semantic_suggestions(facts, bindings, max_examples=max_examples)
    rule_candidates, rule_bindings = _dealer_rule_candidate(
        facts, bindings, valid_evidence_by_company, max_examples=max_examples)
    for situation in SITUATIONS:
        extra, extra_bindings = _dealer_rule_candidate(
            facts, bindings, valid_evidence_by_company, max_examples=max_examples, situation=situation)
        rule_candidates.extend(extra)
        rule_bindings.update(extra_bindings)
    model_candidates = [
        {
            "id": candidate["id"],
            "raw_signal_names": candidate["concept_ids"],
            "facts": [facts[name] for name in candidate["concept_ids"]],
            "company_count": candidate["company_count"],
            "company_ids": candidate["company_ids"],
            "two_verified_facts_company_count": candidate["two_verified_facts_company_count"],
            "distinct_citation_company_count": candidate["distinct_citation_company_count"],
            "independent_source_company_count": candidate["independent_source_company_count"],
            "examples": [
                {"company_id": company_id,
                 "facts": [
                     {**facts[name], "evidence_ids": candidate_bindings[candidate["id"]][company_id][name]["evidence_ids"]}
                     for name in candidate["concept_ids"]]}
                for company_id in candidate["example_company_ids"]
            ],
        }
        for candidate in candidates
    ]
    model_rule_candidates = [
        {**candidate, "examples": [
            {"company_id": company_id,
             "channel": {**rule_bindings[candidate["id"]][company_id]["channel"],
                         "labels": [facts[name]["label"] for name in rule_bindings[candidate["id"]][company_id]["channel"]["raw_signal_names"]]},
             "product_examples": [
                 {"raw_signal_name": name, "label": facts[name]["label"],
                  "evidence_ids": bindings[name][company_id]["evidence_ids"]}
                 for name in rule_bindings[candidate["id"]][company_id]["product"]["raw_signal_names"][:3]]}
            for company_id in candidate["example_company_ids"]]}
        for candidate in rule_candidates
    ]
    summary = {
        "company_count": len(company_ids),
        "companies_with_specific_facts": len(company_facts),
        "exact_concept_count": len(concepts),
        "diagnostic_concept_count": sum(item["diagnostic"] for item in concepts),
        "shared_exact_concept_count": sum(item["company_count"] >= 2 for item in concepts),
        "candidate_count": len(candidates),
        "rule_candidate_count": len(rule_candidates),
        "excluded_nondiagnostic_pair_count": sum(len(members) >= 2 and
            not all(facts[name]["diagnostic"] for name in pair)
            for pair, members in raw_pair_members.items()),
        "two_verified_facts_candidate_count": len(candidates),
        "distinct_citation_candidate_count": sum(item["distinct_citation_company_count"] >= 2 for item in candidates),
        "independent_source_candidate_count": sum(item["independent_source_company_count"] >= 2 for item in candidates),
        "semantic_suggestion_count": len(suggestions),
        "semantic_suggestions_shown": min(len(suggestions), MAX_MODEL_SUGGESTIONS),
        "ignored_common_lexical_anchors": ignored_anchors,
    }
    return {
        "version": 5,
        "summary": summary,
        "concepts": concepts,
        "candidates": candidates,
        "rule_candidates": rule_candidates,
        "semantic_suggestions": suggestions,
        "bindings": {"concepts": dict(bindings), "candidates": candidate_bindings,
                     "rule_candidates": rule_bindings},
        "model_overview": {"summary": summary, "candidates": model_candidates,
                           "rule_candidates": model_rule_candidates,
                           "semantic_suggestions": suggestions[:MAX_MODEL_SUGGESTIONS]},
    }
