"""luca-litellm check: prove the proxy's Luca address and token work, before real traffic."""
import sys

from . import check


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["check"]:
        print("usage: luca-litellm check\n  Sends Luca one test record (never counted as usage) using LUCA_LITELLM_URL and LUCA_LITELLM_TOKEN.")
        return 2
    ok, message = check()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
