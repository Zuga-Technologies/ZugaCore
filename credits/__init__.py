"""ZugaTokens — per-user wallet, Stripe billing, and spend gating.

Public surface for `from core.credits import X`. Long-form module imports
(`core.credits.manager.X`, etc.) continue to work for existing call sites.
"""

from core.credits.client import (
    CreditClient,
    DirectCreditClient,
    HttpCreditClient,
    NullCreditClient,
    ZUGATOKENS_PER_DOLLAR,
    dollars_to_tokens,
    get_credit_client,
)
from core.credits.manager import (
    add_purchased_tokens,
    add_subscription_tokens,
    can_spend,
    get_all_usage,
    get_balance,
    get_transaction_history,
    get_usage,
    grant_tokens,
    issue_welcome_grant_if_new,
    record_spend,
    tokens_to_dollars,
    try_spend,
)
from core.credits.models import (
    CreditLedger,
    Subscription,
    TokenBalance,
    TokenTransaction,
)

__all__ = [
    # client
    "CreditClient",
    "DirectCreditClient",
    "HttpCreditClient",
    "NullCreditClient",
    "ZUGATOKENS_PER_DOLLAR",
    "dollars_to_tokens",
    "get_credit_client",
    # manager
    "add_purchased_tokens",
    "add_subscription_tokens",
    "can_spend",
    "get_all_usage",
    "get_balance",
    "get_transaction_history",
    "get_usage",
    "grant_tokens",
    "issue_welcome_grant_if_new",
    "record_spend",
    "tokens_to_dollars",
    "try_spend",
    # models
    "CreditLedger",
    "Subscription",
    "TokenBalance",
    "TokenTransaction",
]
