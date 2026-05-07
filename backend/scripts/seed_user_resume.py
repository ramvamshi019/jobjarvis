"""
Seed a user + resume + resume embedding from a local resume file.

The simplest possible bootstrap so AI matching has something to match against.
The user has 0 active resumes and 0 resume embeddings — this fixes both.

Usage:
  # 1. copy your resume into the worker container
  docker cp ~/path/to/your_resume.txt jobjarvis_celery_worker:/tmp/resume.txt
  #    (.pdf and .docx also work — auto-extracted)

  docker cp backend/scripts/seed_user_resume.py \\
    jobjarvis_celery_worker:/tmp/seed_user_resume.py

  docker exec jobjarvis_celery_worker python3 -u /tmp/seed_user_resume.py \\
    --email you@example.com \\
    --name "Your Name" \\
    --resume /tmp/resume.txt
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Worker container's HOME is read-only — point HF caches at /tmp BEFORE
# any transformers/sentence-transformers import.
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


def vec_literal(vec) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".txt" or suffix == ".md":
        return path.read_text(errors="ignore")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if suffix == ".docx":
        import docx  # python-docx
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    # Fallback — try as text
    return path.read_text(errors="ignore")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default=None)
    parser.add_argument("--resume", required=True,
                        help="Path inside the container to your resume "
                             "(.txt, .pdf, or .docx)")
    parser.add_argument("--target-role", default="Software Engineer")
    parser.add_argument("--model", default="all-MiniLM-L6-v2")
    args = parser.parse_args()

    path = Path(args.resume)
    if not path.exists():
        print(f"ERROR: resume not found at {path}", flush=True)
        sys.exit(2)

    print(f"Reading resume: {path}", flush=True)
    text = extract_text(path).strip()
    if not text:
        print("ERROR: no text extracted from resume", flush=True)
        sys.exit(2)
    print(f"  extracted {len(text)} characters", flush=True)

    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    # 1. Upsert user
    name = args.name or args.email.split("@")[0]
    user_row = await conn.fetchrow(
        """
        INSERT INTO users (
            email, hashed_password, full_name, role, is_active,
            open_to_remote, notify_email, notify_min_fit_score,
            created_at, updated_at
        ) VALUES (
            $1, 'seed-no-login', $2, 'USER', true,
            true, true, 75,
            NOW(), NOW()
        )
        ON CONFLICT (email) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            is_active = true,
            updated_at = NOW()
        RETURNING id
        """,
        args.email, name,
    )
    user_id = user_row["id"]
    print(f"  user_id = {user_id} ({args.email})", flush=True)

    # 2. Deactivate any existing resume versions for this user
    await conn.execute(
        "UPDATE resume_versions SET is_active = false WHERE user_id = $1",
        user_id,
    )

    # 3. Create new ResumeVersion
    rv_row = await conn.fetchrow(
        """
        INSERT INTO resume_versions (
            user_id, name, target_role, version_tag,
            content, file_path, file_type,
            is_active, created_at, updated_at
        ) VALUES (
            $1, $2, $3, 'v1',
            $4, $5, $6,
            true, NOW(), NOW()
        )
        RETURNING id
        """,
        user_id,
        path.name,
        args.target_role,
        text,
        str(path),
        path.suffix.lstrip(".") or "txt",
    )
    rv_id = rv_row["id"]
    print(f"  resume_version_id = {rv_id}", flush=True)

    # 4. Generate embedding
    print(f"Loading sentence-transformers model (cache: {os.environ['SENTENCE_TRANSFORMERS_HOME']})…",
          flush=True)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(
        args.model,
        cache_folder=os.environ["SENTENCE_TRANSFORMERS_HOME"],
    )
    print(f"  model ready: {args.model}", flush=True)

    # Truncate to model window — first ~3000 chars cover summary + skills
    embed_text = text[:3000]
    vec = model.encode(embed_text, normalize_embeddings=True).tolist()

    # 5. Upsert ResumeEmbedding
    await conn.execute(
        """
        INSERT INTO resume_embeddings (resume_id, model, embedding, created_at)
        VALUES ($1, $2, $3::vector, NOW())
        ON CONFLICT (resume_id) DO UPDATE SET
            model = EXCLUDED.model,
            embedding = EXCLUDED.embedding,
            created_at = NOW()
        """,
        rv_id, args.model, vec_literal(vec),
    )
    print(f"  embedded resume into resume_embeddings ({len(vec)} dims)",
          flush=True)

    # Confirm
    counts = await conn.fetchrow(
        """
        SELECT
          (SELECT COUNT(*) FROM users WHERE is_active=true) AS active_users,
          (SELECT COUNT(*) FROM resume_versions WHERE is_active=true) AS active_resumes,
          (SELECT COUNT(*) FROM resume_embeddings) AS resume_embeds
        """
    )
    print(f"\nFinal: users(active)={counts['active_users']}  "
          f"resumes(active)={counts['active_resumes']}  "
          f"resume_embeds={counts['resume_embeds']}", flush=True)
    print(f"\nNext step: run AI matching for user_id = {user_id}:", flush=True)
    print(f"  docker exec jobjarvis_celery_worker python3 -u "
          f"/tmp/ai_match_jobs.py --user {user_id} --top 50", flush=True)

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
