"""Duplicate detection for machine-failure reports.

How it works (simple version):
1. A new report is compared only with *open* (not resolved) reports for the SAME machine
   that were sent in the last few days.
2. Both texts are cleaned: lower case, no punctuation, no filler words. Then every word is
   replaced by its CONCEPT. Words that mean the same thing become one keyword, for example
   "temperature / heat / hot" -> heat, "reducing / decreased / dropped" -> decrease,
   "rapidly / suddenly / quickly" -> sudden, "noise / sound / rattle" -> noise,
   "smell / smoke / burning" -> smoke. The word groups are in GROUPS below, and you can add
   your own words in the file  synonyms.json  (no code needed).
3. A similarity score between 0 and 1 is calculated:
     - keyword similarity: cosine similarity of the concept keywords (scikit-learn)
     - meaning similarity: sentence embeddings (optional, only if sentence-transformers
       is installed). When available the two scores are blended: 60% meaning + 40% keywords.
     - containment: a SHORT report ("engine overheating") is often fully contained in a LONG
       detailed one. Cosine alone punishes the length difference, so when (almost) all key
       words of the shorter text appear in the longer one AND both describe the same kind of
       problem (overheating, leak, noise ...), the score is raised.
     - opposite directions ("temperature rising" vs "temperature dropping") are different
       problems, so their score is reduced.
4. The score decides the label (limits can be changed in .env):
     score >= 0.75  -> DUPLICATE          (joins the original issue)
     score >= 0.55  -> POSSIBLE DUPLICATE (manager must confirm)
     otherwise      -> NEW ISSUE          (gets its own issue and notes)

Tip: to see exactly how two texts are understood, run
     python check_similarity.py "first text" "second text"
"""
import json
import logging
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta

from .models import Report
from .services import new_issue_for

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- settings
def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def thresholds():
    return (
        _float_env("DUPLICATE_THRESHOLD", 0.75),
        _float_env("POSSIBLE_THRESHOLD", 0.55),
    )


def window_days():
    return int(_float_env("DUPLICATE_WINDOW_DAYS", 7))


def classify(score):
    """Turn a similarity score into 'duplicate', 'possible' or 'new'."""
    duplicate_limit, possible_limit = thresholds()
    if score >= duplicate_limit:
        return "duplicate"
    if score >= possible_limit:
        return "possible"
    return "new"


# ---------------------------------------------------------------- vocabulary
# Words that say nothing about the problem itself.
STOP_WORDS = set(
    """a an the and or but if of to in on at by for with from as into onto over under about
    is are was were be been being am it its itself this that these those there here
    i me my we our you your he she they them their his her
    do does did done doing has have had having can could will would should may might must shall
    not no nor yes very too also just only again still already even ever now then than so such
    when while after before during since until because
    some any all each every more most much many few little lot lots
    please kindly sir madam today yesterday morning evening night
    make makes made making get gets got getting goes going went come comes came
    show shows showed showing seem seems seemed look looks looked looking appear appears
    problem problems issue issues trouble thing things something anything
    wont cant dont doesnt didnt isnt arent wasnt werent cannot
    machine machines machinery robot robots equipment unit device system
    one two three
    become becomes became becoming occur occurs occurred happen happens happened
    cause causes caused causing due regularly often sometimes always never
    strange unusual abnormal weird odd extra unwanted
    heavy heavily severe severely serious seriously bad badly terrible extreme extremely big huge
    around observed observe observes observation require requires required need needs needed
    inspection inspect inspected maintenance urgent urgently attention immediate immediately
    produce produces produced producing generate generates generated generating emit emits emitted
    emitting give gives gave given giving""".split()
)

# Each group is ONE concept. Every word in a group is treated as the same keyword.
# (Add more words here, or - without touching code - in synonyms.json.)
GROUPS = {
    "stop": "stop stops stopped stopping halt halts halted halting shut shuts shutting shutdown "
            "freeze freezes froze frozen hang hangs hung down off dead die dies died trip trips "
            "tripped stall stalls stalled seize seized cease ceased quit quits unresponsive standstill",
    "jam": "jam jams jammed jamming stuck block blocked blockage clog clogged clogging choke "
           "choked obstruct obstructed",
    "quiet": "quiet quieter quietly silent silently silence noiseless noiselessly soundless soundlessly",
    "noise": "noise noises noisy noisier noisiest noisily sound sounds loud loudly louder rattle rattling rattles grind grinding grinds "
             "squeal squealing squeak squeaking buzz buzzing hum humming knock knocking bang banging "
             "clatter clattering clunk clunking screech screeching whine whining hiss hissing "
             "rumble rumbling thump thumping click clicking creak creaking roar roaring howl howling",
    "vibrate": "vibrate vibrates vibrating vibration vibrations shake shakes shaking shaky wobble "
               "wobbling wobbles tremble trembling jitter jittery shudder shuddering oscillate oscillating",
    "leak": "leak leaks leaking leaked leakage drip drips dripping dripped spill spilling spilled "
            "seep seeping seepage ooze oozing trickle trickling puddle",
    "heat": "hot hotter overheat overheats overheating overheated heat heats heating heated "
            "temperature temperatures temp thermal warm warmer boil boiling scorching cold colder chilled",
    "smoke": "smoke smokes smoking smoky fume fumes burning burnt burn burns charred soot flame "
             "flames fire spark sparks sparking smell smells smelling odor odour stink stinks "
             "stinking stench scent",
    "warn": "warning warnings alarm alarms alert alerts error errors fault faults indicator signal "
            "beep beeping buzzer flashing blinking",
    "light": "light lights lamp lamps led bulb",
    "sudden": "sudden suddenly abruptly abrupt unexpectedly unexpected randomly rapid rapidly quick "
              "quickly fast faster swift swiftly instant instantly",
    "power": "power electricity electric supply voltage blackout outage current",
    "broken": "fail fails failed failing failure break breaks broke broken breakdown malfunction "
              "malfunctioning faulty damaged damage damages defect defective crack cracks cracked "
              "cracking fracture fractured snap snapped bent bend worn torn burst",
    "weld": "weld welds welded welding welder welders",
    "slow": "slow slower slowly sluggish lag lagging delay delayed gradual gradually",
    "slip": "slip slips slipped slipping slide slides sliding slippage",
    "increase": "increase increases increased increasing rise rises rising rose raise raised raising "
                "climb climbing climbed spike spiking surge surging high higher excessive jump jumping",
    "decrease": "decrease decreases decreased decreasing reduce reduces reduced reducing reduction "
                "drop drops dropped dropping fall falls falling fell lower lowered lowering dip dips "
                "dipping decline declined declining diminish diminished lessen",
    "pressure": "pressure pressures psi",
}

# Concepts that name the KIND of problem. Two reports can only be "contained" in each other
# when they share one of these (or have no such word at all and match completely).
SYMPTOMS = {"stop", "jam", "noise", "vibrate", "leak", "heat", "smoke", "warn", "slow", "slip"}


# Parts of the machine. Two reports about DIFFERENT parts ("gearbox noise" vs "fan noise") are
# not treated as the same problem. Add your own with "_components" in synonyms.json.
COMPONENTS = set(
    """gearbox gear gears motor pump fan belt bearing bearings valve cylinder tank engine conveyor
    compressor chain clutch brake piston spindle shaft nozzle hose pipe filter radiator battery
    sensor cable wire furnace oven roller press drill lathe cutter blade gripper arm""".split()
)

# Verbs whose past tense / participle does not follow the normal -ed pattern.
IRREGULAR = {
    "ran": "run", "run": "run", "broke": "break", "broken": "break", "froze": "freeze",
    "frozen": "freeze", "shook": "shake", "shaken": "shake", "fell": "fall", "fallen": "fall",
    "rose": "rise", "risen": "rise", "began": "begin", "begun": "begin", "rang": "ring",
    "rung": "ring", "burnt": "burn", "stuck": "stick", "hung": "hang", "wore": "wear",
    "worn": "wear", "tore": "tear", "torn": "tear", "blew": "blow", "blown": "blow",
    "slid": "slide", "sank": "sink", "sunk": "sink", "spilt": "spill", "leapt": "leap",
    "went": "go", "gone": "go", "came": "come", "bent": "bend", "lost": "lose", "caught": "catch",
    "drove": "drive", "driven": "drive", "made": "make", "took": "take", "taken": "take",
    "swelled": "swell", "swollen": "swell", "split": "split", "shut": "shut", "cut": "cut",
}

# Word endings, longest first. They turn noisy / noisily / noises / noisier into one stem, and
# polluted / polluting / pollution / pollutes into one stem. (min = shortest stem left over)
_SUFFIXES = (
    ("ational", 3), ("ations", 3), ("ation", 3), ("ition", 3), ("ments", 3), ("ment", 3),
    ("ness", 3), ("less", 3), ("ings", 3), ("ing", 3), ("ily", 3), ("ied", 3), ("ies", 3),
    ("iest", 3), ("ier", 3), ("ful", 3), ("ers", 4), ("est", 4), ("ive", 4), ("ity", 4),
    ("ion", 4), ("ed", 3), ("es", 3), ("er", 4), ("ly", 3), ("al", 4), ("s", 3), ("y", 3),
)


def _stem(word):
    """Reduce a word to a common stem so that different forms (noun, verb, adjective, adverb,
    any tense) look the same: noise, noisy, noisily, noises -> nois."""
    word = IRREGULAR.get(word, word)
    for _ in range(3):
        for suffix, minimum in _SUFFIXES:
            if word.endswith(suffix) and not word.endswith("ss") and len(word) - len(suffix) >= minimum:
                word = word[: -len(suffix)]
                break
        else:
            break
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "lsz":
        word = word[:-1]
    return word


def _load_extra_groups():
    """Read your own words from synonyms.json (optional). Returns {concept: "word word ..."}.

    File format:  {"leak": "ooze weep", "coolant": "antifreeze radiator", "_symptoms": ["coolant"]}
    Keys starting with "_" are notes, except "_symptoms" (concepts that count as a kind of problem).
    """
    path = os.environ.get("SYNONYMS_FILE") or os.path.join(PROJECT_DIR, "synonyms.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        extra = {}
        for name, words in data.items():
            if name == "_symptoms":
                SYMPTOMS.update(str(w).lower() for w in words)
            elif name == "_components":
                COMPONENTS.update(str(w).lower() for w in words)
            elif not name.startswith("_"):
                text = " ".join(words) if isinstance(words, list) else str(words)
                extra[name.lower()] = text.lower()
        return extra
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("Could not read %s (%s). Using the built-in word groups only.", path, exc)
        return {}


def _build_canon(*group_sets):
    """word -> concept. Earlier group sets win, so your synonyms.json can override the built-ins.
    The simple stem of every group word is added too, so "reducing" also covers "reduces"."""
    canon = {}
    pairs = [(name, word) for groups in group_sets for name, words in groups.items() for word in words.split()]
    for name, word in pairs:
        canon.setdefault(word, name)
    for name, word in pairs:
        canon.setdefault(_stem(word), name)
    return canon


_CANON = _build_canon(_load_extra_groups(), GROUPS)


def normalize(text):
    """Clean text and return a list of concept keywords."""
    text = re.sub(r"[’']", "", (text or "").lower())
    tokens = []
    for word in re.findall(r"[a-z0-9]+", text):
        if word in STOP_WORDS:
            continue
        if word in _CANON:
            tokens.append(_CANON[word])
            continue
        stem = _stem(word)
        if len(stem) < 2 or stem in STOP_WORDS:
            continue
        tokens.append(_CANON.get(stem, stem))
    return tokens


def shared_keywords(text_a, text_b):
    """Keywords that both messages contain (shown to the manager)."""
    return sorted(set(normalize(text_a)) & set(normalize(text_b)))


# ---------------------------------------------------------------- similarity
def _lexical_scores(new_text, other_texts):
    """Keyword similarity (cosine on concept keywords) of one text against many."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vectorizer = TfidfVectorizer(
            tokenizer=normalize, lowercase=False, token_pattern=None, use_idf=False, binary=True
        )
        matrix = vectorizer.fit_transform([new_text] + list(other_texts))
        return [float(x) for x in cosine_similarity(matrix[0], matrix[1:])[0]]
    except ValueError:  # nothing left after cleaning
        return [0.0] * len(other_texts)
    except ImportError:  # scikit-learn missing: same maths in plain Python
        base = Counter(set(normalize(new_text)))
        scores = []
        for text in other_texts:
            other = Counter(set(normalize(text)))
            common = sum(base[k] * other[k] for k in base)
            denom = math.sqrt(len(base)) * math.sqrt(len(other))
            scores.append(common / denom if denom else 0.0)
        return scores


CONTAIN_WEIGHT = 0.95  # best possible score from the containment rule
OPPOSITE_FACTOR = 0.6  # score is multiplied by this when one text says "up" and the other "down"
DIFFERENT_PROBLEM_FACTOR = 0.6  # ... or when both name a problem type and the types differ
MIN_CONTAINED = 0.75  # the shorter text must be at least this much inside the longer one


def _containment(tokens_a, tokens_b):
    """0..CONTAIN_WEIGHT: how much of the SHORTER keyword set is found in the other text."""
    a, b = set(tokens_a), set(tokens_b)
    if min(len(a), len(b)) < 2:  # a single word says too little
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    sym_a, sym_b = a & SYMPTOMS, b & SYMPTOMS
    if sym_a and sym_b and not (sym_a & sym_b):  # different kinds of problem
        return 0.0
    small = a if len(a) <= len(b) else b
    if not (shared & SYMPTOMS) and not (len(small) >= 3 and shared == small):
        return 0.0

    def weight(words):  # problem-type words count double
        return sum(2 if w in SYMPTOMS else 1 for w in words)

    ratio = weight(shared) / weight(small)
    if ratio < MIN_CONTAINED:  # e.g. "gearbox noise" vs "motor noise": only the symptom is shared
        return 0.0
    return CONTAIN_WEIGHT * ratio


def _opposite_direction(tokens_a, tokens_b):
    """True when one text says 'going up' and the other 'going down' (or 'noisy' vs 'quiet')."""
    a, b = set(tokens_a), set(tokens_b)
    a_up, a_down = "increase" in a, "decrease" in a
    b_up, b_down = "increase" in b, "decrease" in b
    if (a_up and not a_down and b_down and not b_up) or (a_down and not a_up and b_up and not b_down):
        return True
    return ("quiet" in a and "noise" in b and "noise" not in a) or ("quiet" in b and "noise" in a and "noise" not in b)


def _different_problem(tokens_a, tokens_b):
    """True when both texts name a kind of problem (noise, leak, stop ...) but not the same one,
    or when they name different machine parts (gearbox vs fan)."""
    sym_a, sym_b = set(tokens_a) & SYMPTOMS, set(tokens_b) & SYMPTOMS
    if sym_a and sym_b and not (sym_a & sym_b):
        return True
    return _different_part(tokens_a, tokens_b)


def _different_part(tokens_a, tokens_b):
    stems = {_stem(c) for c in COMPONENTS}
    part_a, part_b = set(tokens_a) & stems, set(tokens_b) & stems
    return bool(part_a and part_b and not (part_a & part_b))


SAME_SYMPTOM_SCORE = 0.76  # same kind of problem, nothing in the texts contradicts it


def _same_symptom(tokens_a, tokens_b):
    """Score for two reports that name exactly the same kind of problem (e.g. both are about
    noise) and do not contradict each other. Different wording around it (the cause, the place,
    pollution, overwork ...) does not matter. 0 when it does not apply."""
    sym_a, sym_b = set(tokens_a) & SYMPTOMS, set(tokens_b) & SYMPTOMS
    if not sym_a or sym_a != sym_b:
        return 0.0
    if _opposite_direction(tokens_a, tokens_b) or _different_part(tokens_a, tokens_b):
        return 0.0
    return SAME_SYMPTOM_SCORE


_model = None
_model_failed = False


def _get_model():
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed or os.environ.get("USE_EMBEDDINGS", "1") == "0":
        return None
    try:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(os.environ.get("DUPLICATE_MODEL", "all-MiniLM-L6-v2"))
    except Exception as exc:  # not installed, offline, unsupported Python ...
        log.warning("Sentence-transformer model not available (%s). Using keyword matching only.", exc)
        _model_failed = True
        return None
    return _model


def _semantic_scores(new_text, other_texts):
    model = _get_model()
    if model is None:
        return None
    try:
        vectors = model.encode([new_text] + list(other_texts), normalize_embeddings=True)
        return [max(0.0, float(v @ vectors[0])) for v in vectors[1:]]
    except Exception as exc:
        log.warning("Embedding comparison failed (%s). Using keyword matching only.", exc)
        return None


def similarity_scores(new_text, other_texts):
    """Similarity (0..1) of new_text against every text in other_texts."""
    if not other_texts:
        return []
    base = normalize(new_text)
    others = [normalize(text) for text in other_texts]
    lexical = _lexical_scores(new_text, other_texts)
    contained = [max(_containment(base, tokens), _same_symptom(base, tokens)) for tokens in others]
    semantic = _semantic_scores(new_text, other_texts)
    if semantic is None:
        scores = [max(l, c) for l, c in zip(lexical, contained)]
    else:
        scores = [max(0.6 * s + 0.4 * l, c) for s, l, c in zip(semantic, lexical, contained)]
    result = []
    for score, tokens in zip(scores, others):
        if _opposite_direction(base, tokens) or _different_problem(base, tokens):
            score *= OPPOSITE_FACTOR
        result.append(score)
    return result


# ---------------------------------------------------------------- main entry points
def find_best_match(report):
    """Return (best_matching_report, score) among open reports of the same machine."""
    cutoff = datetime.now() - timedelta(days=window_days())
    candidates = (
        Report.query.filter(
            Report.machine_id == report.machine_id,
            Report.id != report.id,
            Report.status != "Resolved",
            Report.created_at >= cutoff,
        )
        .order_by(Report.created_at.desc())
        .all()
    )
    if not candidates:
        return None, 0.0
    scores = similarity_scores(report.text, [c.text for c in candidates])
    best_index = max(range(len(scores)), key=lambda i: scores[i])
    return candidates[best_index], scores[best_index]


def assign_issue(report):
    """Compare a freshly saved report with open ones and attach it to an issue.

    Call this after db.session.add(report) + db.session.flush(). Returns the label.
    """
    match, score = find_best_match(report)
    label = classify(score) if match else "new"
    report.similarity_score = round(score, 4) if match else None

    if label == "duplicate":
        report.match_status = "duplicate"
        report.duplicate_of_id = match.id
        report.issue = match.issue
        report.status = match.issue.status
        match.issue.recount()
    elif label == "possible":
        report.match_status = "possible"
        report.duplicate_of_id = match.id
        new_issue_for(report)
    else:
        report.match_status = "new"
        report.duplicate_of_id = None
        new_issue_for(report)
    return label