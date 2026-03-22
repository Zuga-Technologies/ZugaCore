# ZugaCore

Shared foundation layer for the [Zuga](https://github.com/Zuga-Technologies) studio ecosystem. Provides authentication, database management, a credit/token system, a Tailwind theme, and plugin interfaces used by all Zuga studios.

## What's Inside

### Backend (Python)

| Module | Purpose |
|--------|---------|
| `auth/` | Email + password + Google OAuth login, session tokens, middleware |
| `database/` | SQLAlchemy async engine, session management, auto-migration |
| `credits/` | Token wallet (3-bucket: free, subscription, purchased), spend tracking |
| `plugins/` | `StudioPlugin` and `ProxyPlugin` abstract base classes |

### Frontend (TypeScript)

| Module | Purpose |
|--------|---------|
| `frontend/api/client.ts` | Fetch wrapper with Bearer auth + auto-logout on 401 |
| `frontend/auth/store.ts` | Pinia auth store (login, logout, Google OAuth, session check) |
| `frontend/plugins/interface.ts` | `StudioFrontendPlugin` type definition |
| `frontend/theme/` | Tailwind preset (colors, animations) + base CSS (glass cards, buttons) |

### Scripts

| File | Purpose |
|------|---------|
| `scripts/setup-symlinks.sh` | Creates backend symlinks for studios (cross-platform) |

## Usage

ZugaCore is designed to be included as a **git submodule** in studio repos:

```bash
# In your studio repo
git submodule add https://github.com/Zuga-Technologies/ZugaCore.git core
```

### Frontend Integration

Configure your Vite alias to point at the submodule:

```typescript
// vite.config.ts
resolve: {
  alias: {
    '@core': resolve(__dirname, '../core/frontend'),
  },
}
```

Then import anywhere:

```typescript
import { useAuthStore } from '@core/auth/store'
import { api } from '@core/api/client'
import type { StudioFrontendPlugin } from '@core/plugins/interface'
```

### Backend Integration

Create symlinks so Python can resolve `from core.xxx`:

```bash
# setup.sh handles this, or manually:
ln -s ../core/auth     backend/core/auth
ln -s ../core/database backend/core/database
ln -s ../core/credits  backend/core/credits
ln -s ../core/plugins  backend/core/plugins
```

Then import anywhere:

```python
from core.auth.middleware import get_current_user
from core.database.session import get_session, init_engine
from core.database.base import Base, TimestampMixin
from core.credits.client import get_credit_client
from core.plugins.interface import StudioPlugin
```

## Plugin Types

### StudioPlugin (embedded)

Runs inside ZugaApp, shares its database and auth. The studio provides a FastAPI router that gets mounted into the host app.

```python
class MyStudioPlugin(StudioPlugin):
    name = "mystudio"
    version = "1.0.0"

    @property
    def router(self) -> APIRouter:
        return my_router
```

### ProxyPlugin (standalone)

For studios that run their own backend 24/7. ZugaApp forwards requests to the standalone service.

```python
class MyTraderPlugin(ProxyPlugin):
    name = "trader"
    version = "0.1.0"
    proxy_to = "http://localhost:8002"
    prefix = "/api/trader"
```

## Auth Modes

| Mode | Behavior |
|------|----------|
| `dev` | Any email accepted, no verification — for local development |
| `password` | Email + password registration with email verification |
| `google` | Google OAuth (requires `GOOGLE_CLIENT_ID` env var) |

Set via `AUTH_MODE` environment variable.

## Credit System

Three operating modes based on environment:

| Scenario | Client Used | How |
|----------|-------------|-----|
| Inside ZugaApp | `DirectCreditClient` | Shares ZugaApp's database |
| Standalone + `ZUGAAPP_CREDITS_URL` set | `HttpCreditClient` | Calls ZugaApp's credit API |
| Standalone + no URL | `DirectCreditClient` | Uses own SQLite with 50 free daily tokens |

`CREDIT_FAIL_MODE=open` (default) allows spending when the credit server is unreachable.

## Requirements

- Python 3.10+
- Node.js 18+ (for frontend consumers)

```bash
pip install -r requirements.txt
```

## Studios Using ZugaCore

- [ZugaLife](https://github.com/Zuga-Technologies/ZugaLife) — Wellness and life tracking
- More coming soon

## License

[MIT](LICENSE) - Zuga Technologies
