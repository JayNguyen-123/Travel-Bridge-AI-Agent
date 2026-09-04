import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Tests must never touch real services. These placeholder env vars satisfy
# module-level `stripe.api_key = os.environ.get(...)`-style reads without a
# real .env file being present in CI.
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("AMADEUS_ACCESS_TOKEN", "test_placeholder")
os.environ.setdefault("AMADEUS_BASE_URL", "https://test.api.amadeus.com")
