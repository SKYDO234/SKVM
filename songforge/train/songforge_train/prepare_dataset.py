"""CLI wrapper: ``python -m songforge_train.prepare_dataset raw/ cache/``."""

from .dataset import main

if __name__ == "__main__":
    raise SystemExit(main())
