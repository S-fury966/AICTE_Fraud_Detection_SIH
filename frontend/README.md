# Authentiq — reviewer dashboard

React 19 + TypeScript + Tailwind 4, built with Vite. Reads everything from the
backend API; there is no mock data in the frontend.

```bash
npm install
npm run dev            # http://localhost:5175, proxies /api to 127.0.0.1:8765
API_TARGET=http://127.0.0.1:8800 npm run dev    # different API port
npm run build          # type-check + production build into dist/
```

Start the API first: `cd ../backend && python -m app.api`. Or run everything
with `../run_demo.sh`.

Identity: the header's reviewer name and role are sent as `X-Actor` / `X-Role`
on every request. There is no login in the prototype (a confirmed decision);
the API validates the role, enforces permissions and records it in the audit
trail.

Deep links: `/applications/<id>?tab=certificate|faculty|photos|audit`.

Colour: risk and strength use a fixed status palette (critical / serious /
warning / neutral) and always carry a text label and glyph, never colour alone.
