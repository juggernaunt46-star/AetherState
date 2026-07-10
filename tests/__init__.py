# Regular-package marker. Without this, `from tests.mock_upstream import ...` can be
# hijacked by any site-packages distribution that ships a top-level `tests` package
# (ultralytics does) — regular packages beat namespace packages regardless of path order.
