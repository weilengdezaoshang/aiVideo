"""Serve compiled TypeScript at the existing .js module URLs, with legacy static fallback."""

from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


class FrontendFiles(StaticFiles):
    def __init__(self, root: Path):
        super().__init__(directory=root / "apps" / "web", html=True, check_dir=False)
        self.compiled = (root / ".web-build").resolve()
        self.source = (root / "apps" / "web").resolve()

    async def get_response(self, path, scope):
        if path.endswith((".js", ".js.map")):
            compiled = (self.compiled / path).resolve()
            if compiled.is_relative_to(self.compiled) and compiled.is_file():
                return FileResponse(compiled, headers={"Cache-Control": "no-cache"})
            for suffix in (".ts", ".tsx"):
                source = (self.source / path).with_suffix(suffix).resolve()
                if source.is_relative_to(self.source) and source.is_file():
                    return JSONResponse({"error": "请先运行 npm run build:web"}, status_code=503)
        response = await super().get_response(path, scope)
        # Application HTML/CSS/legacy JS must revalidate together with compiled modules.
        # Keep normal caching for vendored libraries and media assets.
        if not path.startswith("vendor/") and (path.endswith((".css", ".js", ".html")) or "." not in Path(path).name):
            response.headers["Cache-Control"] = "no-cache"
        return response
