# Architecture Decisions

Per Section 7 (Autonomy Clause), documenting deviations from the spec.

- **Frontend: Jinja2 + HTMX instead of React.** Rationale: no build step, one deployment artifact, faster iteration, same interactivity for this use case. Chart.js handles sparklines and charts client-side.
- **Database: PostgreSQL (kept).** Rationale: Railway containers are ephemeral — SQLite would lose data on redeploy. PostgreSQL is included in Railway Hobby plan at no extra cost.
- **CSS: Custom stylesheet instead of Tailwind.** Rationale: exact control over Elusive design tokens from Section 4, no build toolchain, CDN Tailwind adds 300KB+ for no benefit at this scale.
- **Auth: Authlib for OAuth flows.** Rationale: well-maintained, handles Google OAuth 2.0 + token refresh cleanly, avoids rolling custom OAuth.
- **Scheduler: APScheduler (in-process).** Rationale: no external dependency, sufficient for 6-hour refresh cycle with 7 creators.
- **Session store: Server-side encrypted cookies via Starlette.** Rationale: stateless, no Redis/memcache needed for single-digit concurrent users.
