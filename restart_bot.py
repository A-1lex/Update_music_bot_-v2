"""Compatibility entry point: restart management now belongs to the supervisor."""
from supervisor import main

if __name__ == "__main__":
    raise SystemExit(main())
