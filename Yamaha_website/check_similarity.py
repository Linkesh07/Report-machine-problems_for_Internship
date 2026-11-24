"""See how the portal understands two reports and how similar it thinks they are.

Usage (from the project folder, with the venv turned on):
    python check_similarity.py "temperature is reducing rapidly" "heat is suddenly decreased"
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from app.duplicate_detection import (  # noqa: E402
    _get_model, classify, normalize, shared_keywords, similarity_scores, thresholds,
)


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    a, b = sys.argv[1], sys.argv[2]
    duplicate_limit, possible_limit = thresholds()
    score = similarity_scores(a, [b])[0]
    method = "keywords + AI meaning model" if _get_model() is not None else "keywords only (AI model not installed)"

    print(f"\nText 1 understood as: {sorted(set(normalize(a)))}")
    print(f"Text 2 understood as: {sorted(set(normalize(b)))}")
    print(f"Words in common     : {shared_keywords(a, b)}")
    print(f"Method              : {method}")
    print(f"Similarity          : {score:.0%}")
    label = {"duplicate": "DUPLICATE", "possible": "POSSIBLE DUPLICATE (manager decides)", "new": "NEW ISSUE"}[classify(score)]
    print(f"Result              : {label}   (duplicate from {duplicate_limit:.0%}, possible from {possible_limit:.0%})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
