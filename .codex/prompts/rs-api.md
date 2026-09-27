---
description: Add or change a FastAPI endpoint end to end.
---
Follow `.agents/playbooks/api-endpoint.md`.

Router validates and delegates; logic lives in `backend/app/services`; schema in
`backend/app/schemas`; frontend call in `frontend/src/lib/api.ts` and types in
`frontend/src/types`. Test in `backend/tests/test_<area>.py`.

Task: $ARGUMENTS
