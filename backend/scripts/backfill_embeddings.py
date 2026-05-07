"""
Backfill embeddings for all active jobs that don't have one yet.

Bypasses Celery (their _run_async wrapper has a fork+asyncpg deadlock).
Talks asyncpg directly + sentence-transformers all-MiniLM-L6-v2 (384 dims, free,
runs on CPU in the worker container that already has the model cached).

Usage:
  docker cp backend/scripts/backfill_embeddings.py \\
    jobjarvis_celery_worker:/tmp/backfill_embeddings.py
  docker exec jobjarvis_celery_worker python3 -u /tmp/backfill_embeddings.py
  # → embeds in batches of 200, idempotent, safe to re-run

Optional flags:
  --limit N    embed at most N jobs this run (default: all)
  --batch  N   batch size (default: 200)
  --model NAME model name to record (default: all-MiniLM-L6-v2)
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

# Worker container's HOME (/home/appuser) is read-only — point HF caches at /tmp
# BEFORE any transformers/sentence-transformers import.
os.environ.setdefault("HF_HOME", "/tmp/hf_cache")
os.environ.setdefault("TRANSFORMERS_CACHE", "/tmp/hf_cache/transformers")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", "/tmp/st_cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")
for _d in (os.environ["HF_HOME"], os.environ["SENTENCE_TRANSFORMERS_HOME"]):
    os.makedirs(_d, exist_ok=True)

import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


# ── Embedding text builder (mirrors app/services/embedding_service.py) ────────

def build_text(title: str, description: str, skills, location: str) -> str:
    parts = []
    if title:
        parts.append(f"Job title: {title.strip()}")
        parts.append(f"Role: {title.strip()}")
    if location:
        parts.append(f"Location: {location.strip()}")
    if skills:
        skills_list = skills if isinstance(skills, list) else []
        if skills_list:
            parts.append(f"Required skills: {', '.join(str(s) for s in skills_list[:20])}")
    if description:
        parts.append(description.strip()[:1500])
    return "\n".join(parts)


# ── pgvector serialization ────────────────────────────────────────────────────

def vec_literal(vec) -> str:
    """Serialize a Python list[float] into the pgvector text format."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--model", type=str, default="all-MiniLM-L6-v2")
    args = parser.parse_args()

    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    # Count what's left to do
    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM jobs j
        WHERE j.active = true
          AND NOT EXISTS (
            SELECT 1 FROM job_embeddings je WHERE je.job_id = j.id
          )
        """
    )
    print(f"Jobs needing embedding: {total}", flush=True)
    if total == 0:
        print("Nothing to do. ✔", flush=True)
        await conn.close()
        return

    cap = min(args.limit, total) if args.limit else total
    print(f"Will embed up to {cap} jobs in batches of {args.batch}\n", flush=True)

    # Lazy-load the model — heavy (~80MB) but cached on disk after first run
    print(f"Loading sentence-transformers model (cache: {os.environ['SENTENCE_TRANSFORMERS_HOME']})…",
          flush=True)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(
        args.model,
        cache_folder=os.environ["SENTENCE_TRANSFORMERS_HOME"],
    )
    dims = model.get_sentence_embedding_dimension()
    print(f"  model ready: {args.model} ({dims} dims)\n", flush=True)

    embedded_total = 0
    failed = 0
    t0 = time.time()

    while embedded_total < cap:
        # Pull next batch of jobs without embeddings
        rows = await conn.fetch(
            """
            SELECT j.id, j.title, j.description, j.location,
                   j.required_skills, j.preferred_skills
            FROM jobs j
            WHERE j.active = true
              AND NOT EXISTS (
                SELECT 1 FROM job_embeddings je WHERE je.job_id = j.id
              )
            ORDER BY j.id
            LIMIT $1
            """,
            min(args.batch, cap - embedded_total),
        )
        if not rows:
            break

        # Build text + encode in one batch (fast on CPU)
        texts = []
        ids = []
        for r in rows:
            req = r["required_skills"] or []
            pref = r["preferred_skills"] or []
            # asyncpg returns json fields as strings; json column = list/dict
            if isinstance(req, str):
                try: req = __import__("json").loads(req)
                except Exception: req = []
            if isinstance(pref, str):
                try: pref = __import__("json").loads(pref)
                except Exception: pref = []
            text = build_text(
                r["title"] or "",
                r["description"] or "",
                (req or []) + (pref or []),
                r["location"] or "",
            )
            texts.append(text or (r["title"] or "untitled"))
            ids.append(r["id"])

        try:
            vectors = model.encode(
                texts,
                batch_size=64,
                show_progress_bar=False,
                normalize_embeddings=True,
            )
        except Exception as e:
            print(f"  encode failed for batch starting at job {ids[0]}: {e}",
                  flush=True)
            failed += len(rows)
            embedded_total += len(rows)  # avoid infinite loop
            continue

        # Insert into job_embeddings (cast text → vector on the SQL side)
        records = [
            (job_id, args.model, vec_literal(list(vec)))
            for job_id, vec in zip(ids, vectors)
        ]

        try:
            await conn.executemany(
                """
                INSERT INTO job_embeddings (job_id, model, embedding, created_at)
                VALUES ($1, $2, $3::vector, NOW())
                ON CONFLICT (job_id) DO UPDATE SET
                    model = EXCLUDED.model,
                    embedding = EXCLUDED.embedding,
                    created_at = NOW()
                """,
                records,
            )
            embedded_total += len(records)
        except Exception as e:
            print(f"  insert failed for batch starting at job {ids[0]}: {e}",
                  flush=True)
            failed += len(records)
            embedded_total += len(records)
            continue

        elapsed = time.time() - t0
        rate = embedded_total / elapsed if elapsed else 0
        remaining = cap - embedded_total
        eta_s = remaining / rate if rate else 0
        print(f"  embedded {embedded_total}/{cap}  "
              f"({rate:.1f} jobs/s, ETA {eta_s/60:.1f}m)", flush=True)

    elapsed = time.time() - t0
    print(f"\n=== DONE ===", flush=True)
    print(f"  embedded:  {embedded_total - failed}", flush=True)
    print(f"  failed:    {failed}", flush=True)
    print(f"  elapsed:   {elapsed/60:.1f}m  ({embedded_total/elapsed:.1f} jobs/s)",
          flush=True)

    # Show coverage
    after = await conn.fetchrow(
        """
        SELECT
          (SELECT COUNT(*) FROM jobs WHERE active=true) AS active_jobs,
          (SELECT COUNT(*) FROM job_embeddings)        AS embedded
        """
    )
    print(f"\nCoverage: {after['embedded']} / {after['active_jobs']} active jobs "
          f"({100.0*after['embedded']/after['active_jobs']:.1f}%)", flush=True)

    await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
