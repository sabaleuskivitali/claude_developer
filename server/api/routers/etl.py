from fastapi import APIRouter, Depends, Request
from auth import require_api_key

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.get("/etl")
async def etl_status(request: Request, limit: int = 20):
    rows = await request.app.state.db.fetch(
        """SELECT run_at, files, rows, duration_ms, error
           FROM etl_status
           ORDER BY id DESC
           LIMIT $1""",
        limit,
    )
    runs = [
        {
            "run_at":      r["run_at"].isoformat(),
            "files":       r["files"],
            "rows":        r["rows"],
            "duration_ms": r["duration_ms"],
            "status":      "error" if r["error"] else "ok",
            "error":       r["error"],
        }
        for r in rows
    ]
    total_rows = sum(r["rows"] for r in runs)
    return {"runs": runs, "count": len(runs), "total_rows_loaded": total_rows}
