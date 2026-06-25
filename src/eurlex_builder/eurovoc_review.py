"""Interactive EuroVoc concept review for descriptive mode."""

from __future__ import annotations


def review_eurovoc_matches(
    eurovoc_map: dict[str, dict[str, set[str]]],
    source,
) -> list[str]:
    """Walk the user through matched EuroVoc concepts one by one.

    For each concept the user can:
      (1) Include it in the filter
      (2) Exclude it
      (3) Show the EuroVoc thesaurus definition and related concepts,
          then decide

    Returns the final list of accepted concept URIs.
    """
    accepted: set[str] = set()
    total = sum(len(concepts) for concepts in eurovoc_map.values())
    i = 0

    for keyword, concepts in eurovoc_map.items():
        if not concepts:
            print(f"\n  Keyword '{keyword}' — no EuroVoc matches.")
            continue

        print(f"\n  Keyword '{keyword}' — {len(concepts)} match(es):\n")

        for uri, labels in sorted(concepts.items(), key=lambda x: min(x[1])):
            i += 1
            labels_str = ", ".join(sorted(labels))
            print(f"  [{i}/{total}] {labels_str}")
            print(f"          {uri}")

            choice = _prompt_123(
                "          (1) Include  (2) Exclude  (3) Definition"
            )

            if choice == "1":
                accepted.add(uri)
            elif choice == "3":
                _show_definition(uri, source)
                # After seeing the definition, ask include/exclude.
                choice2 = _prompt_12(
                    "          (1) Include  (2) Exclude"
                )
                if choice2 == "1":
                    accepted.add(uri)

    print(f"\n  Accepted {len(accepted)} of {total} EuroVoc concept(s).\n")
    return sorted(accepted)


def _show_definition(concept_uri: str, source) -> None:
    """Fetch and display the EuroVoc thesaurus entry for a concept."""
    info = source.fetch_concept_info(concept_uri)
    if not info:
        print("          (no thesaurus information available)")
        return

    if info.get("definition"):
        print(f"          Definition: {info['definition'][0]}")
    else:
        print("          Definition: (none available)")

    if info.get("broader"):
        print(f"          Broader:    {', '.join(info['broader'])}")
    if info.get("narrower"):
        print(f"          Narrower:   {', '.join(info['narrower'])}")
    if info.get("related"):
        print(f"          Related:    {', '.join(info['related'])}")

    print()


def _prompt_123(prompt: str) -> str:
    """Prompt for 1/2/3, default 1."""
    while True:
        raw = input(f"{prompt} [1]: ").strip()
        if raw in ("", "1"):
            return "1"
        if raw == "2":
            return "2"
        if raw == "3":
            return "3"
        print("          Please enter 1, 2, or 3.")


def _prompt_12(prompt: str) -> str:
    """Prompt for 1/2, default 1."""
    while True:
        raw = input(f"{prompt} [1]: ").strip()
        if raw in ("", "1"):
            return "1"
        if raw == "2":
            return "2"
        print("          Please enter 1 or 2.")
