# Backend Engineering Skill

Standards for building reliable, testable backends. Read this BEFORE writing any backend code.

> Pairs with: `frontend-engineering-skill.md` — the frontend consumes the error/response formats defined here.

## API Response Format (CANONICAL)

All API endpoints MUST return this envelope:

```
Success: {data: T, meta?: {total: number, page: number, limit: number, pages: number}}
Error:   {error: {code: string, message: string, details?: [{field: string, message: string}]}}
```

- Single resource: `{data: {id: 1, name: "..."}}`
- List: `{data: [...], meta: {total: 42, page: 1, limit: 20, pages: 3}}`
- Error: `{error: {code: "VALIDATION_ERROR", message: "Invalid input", details: [{field: "email", message: "Required"}]}}`
- NEVER return bare arrays, bare objects, or `{success: false}` wrappers

## Pagination

Any list endpoint that could grow beyond 20 items MUST support pagination:

```
GET /api/items?page=1&limit=20
→ {data: [...], meta: {total, page, limit, pages}}
```

Default: `page=1, limit=20`. Frontend expects `meta` to render pagination controls.

## Input Validation

Every POST/PUT endpoint MUST:

1. Validate ALL required fields — empty string is NOT valid for required fields
2. Validate types and constraints (email format, positive numbers, string length)
3. Return 400 with field-level errors so the frontend can show inline messages:
   ```json
   {
     "error": {
       "code": "VALIDATION_ERROR",
       "message": "Invalid input",
       "details": [
         {"field": "email", "message": "Valid email required"},
         {"field": "name", "message": "Name is required"}
       ]
     }
   }
   ```
4. Backend MUST re-validate even if frontend validates — never trust client input

## HTTP Status Codes

Use consistently across all endpoints:

- `200` — success (GET, PUT, PATCH)
- `201` — created (POST that creates a resource)
- `400` — validation error (with `details` array)
- `404` — resource not found
- `409` — conflict (duplicate unique field, concurrent edit)
- `500` — server error (log full trace, return generic message to client)

NEVER return HTML error pages from API endpoints. NEVER return 200 for errors.

## External Service Fallback

AI/LLM features and external APIs MUST work without credentials:

- Check for API key at **request time**, not at startup — the app must boot regardless
- When API key is missing: return 200 with mock/fallback data, NOT 503
- Mock data must be realistic and contextual (statistical analysis, pattern-based suggestions), not "lorem ipsum"
- Log a warning: `"AI features running in mock mode — set ANTHROPIC_API_KEY for full functionality"`
- The UI must show the same components for mock and real data — the user shouldn't notice the difference

## Real-time Communication

### Decision tree

- Need bidirectional messages? → **WebSocket**
- Server-push only (notifications, status updates)? → **SSE** (simpler, HTTP-native)
- Low frequency updates (<1/min)? → **Polling** (simplest, most reliable)

### WebSocket implementation checklist

A WebSocket feature is NOT "implemented" until ALL of these work:

- [ ] Client connects — verify via console log or connection indicator in UI
- [ ] Client sends message → server receives and processes it
- [ ] Server broadcasts → ALL connected clients receive the message
- [ ] Received messages update the DOM (not just console.log)
- [ ] Reconnection on disconnect with exponential backoff
- [ ] Connection cleanup on page unload (`beforeunload` event)
- [ ] Test: open two browser tabs, action in tab A appears in tab B

## Anti-Patterns (NEVER do these)

- Endpoint described in spec but not implemented → returns 404 → automatic FAIL
- Accepting empty or invalid POST/PUT body silently (200 OK with garbage data)
- AI endpoint returning 503 "Service Unavailable" with no fallback
- WebSocket that connects but never sends or processes messages
- Different error response shapes from different endpoints
- Returning 200 for failed operations with `{success: false, error: "..."}`
- HTML error pages from API endpoints (FastAPI default 422 page)
- Startup crash when optional env vars (API keys) are missing
