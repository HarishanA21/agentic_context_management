## Summary

<!-- What does this PR change and why? 1-3 bullets. -->

-
-

## Type of change

- [ ] Feature
- [ ] Bug fix
- [ ] Refactor
- [ ] Docs
- [ ] CI / tooling

## Test plan

<!-- How did you verify this works? -->

- [ ] Backend starts: `cd backend && uvicorn api:app --reload --port 8000`
- [ ] Frontend builds: `cd ui && npm run build`
- [ ] Manual smoke test in browser at `localhost:3000`
- [ ] Tool calls (if touched): exercised end-to-end

## Checklist

- [ ] No secrets / `.env` files committed
- [ ] New tools registered in [backend/Tools/__init__.py](../backend/Tools/__init__.py)
- [ ] Auth-protected endpoints use `Depends(get_current_user)`
- [ ] DB queries scope by `user_id`
- [ ] Frontend uses `authFetch` for protected routes
- [ ] [README.md](README.md) updated if setup / architecture changed

## Screenshots (UI changes only)

<!-- Drag screenshots here -->
