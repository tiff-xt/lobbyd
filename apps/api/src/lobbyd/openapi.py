"""Print the API's OpenAPI document: `python -m lobbyd.openapi`.

The web client's TypeScript types are generated from this, so the two cannot
drift apart without a type error.
"""

import json
import sys

from lobbyd.main import create_app


def main() -> None:
    json.dump(create_app().openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
