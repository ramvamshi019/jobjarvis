"""Resume version endpoints — upload, list, activate, auto-embed, auto-match."""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, text

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.resume import ResumeVersion
from app.models.ai_models import ResumeEmbedding
from app.services.resume_parser import parse_resume_from_bytes
from app.services.embedding_service import generate_embedding
from app.services.match_service import recompute_matches_for_user

router = APIRouter(prefix="/resumes", tags=["resumes"])


def _vec_literal(vec) -> str:
    """Serialize list[float] → pgvector text format `[...,...]`."""
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


@router.post("/upload", status_code=201)
async def upload_resume(
    file: UploadFile = File(...),
    target_role: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upload a resume → parse → store → embed → activate → trigger AI matching.

    The whole pipeline runs synchronously so the user can immediately see
    matches on the next page load.
    """
    content = await file.read()
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "txt"
    parsed = parse_resume_from_bytes(content, ext)

    # Persist the raw file to disk so Playwright auto-apply can re-upload it.
    # /app/uploads MUST be a mounted volume in production (see prod
    # docker-compose); the old /tmp fallback silently lost user data on
    # container restart, so we now fail loudly if the canonical path isn't
    # writable rather than writing to non-persistent storage.
    import os
    from fastapi import HTTPException
    user_dir = f"/app/uploads/{current_user.id}"
    try:
        os.makedirs(user_dir, exist_ok=True)
        # Probe-write a marker file to confirm the volume is actually writable
        marker = f"{user_dir}/.write_test"
        with open(marker, "w") as _f:
            _f.write("ok")
        os.remove(marker)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "Resume storage is not writable. "
                "Check that /app/uploads is mounted as a persistent volume."
                f" ({e})"
            ),
        )
    saved_path = f"{user_dir}/resume.{ext}"
    with open(saved_path, "wb") as fh:
        fh.write(content)

    # 1. Deactivate any existing resumes for this user
    await db.execute(
        update(ResumeVersion)
        .where(ResumeVersion.user_id == current_user.id)
        .values(is_active=False)
    )

    # 2. Insert new resume as active
    resume = ResumeVersion(
        user_id=current_user.id,
        name=name or file.filename,
        target_role=target_role,
        content=parsed.raw_text,
        file_path=saved_path,
        file_type=ext,
        skills_json=parsed.skills,
        tools_json=parsed.tools,
        cloud_platforms_json=parsed.cloud_platforms,
        certifications_json=parsed.certifications,
        experience_level=parsed.experience_level,
        overall_strength_score=parsed.overall_strength_score,
        parsed_at=datetime.now(timezone.utc),
        is_active=True,
    )
    db.add(resume)
    await db.commit()
    await db.refresh(resume)

    # 3. Generate embedding (sentence-transformers all-MiniLM-L6-v2, 384 dims)
    #    Build canonical text from skills + summary + raw text.
    skills_text = ", ".join(parsed.skills or [])
    embed_text = (
        f"Skills: {skills_text}\n"
        f"Target role: {target_role or ''}\n"
        f"{(parsed.raw_text or '')[:3000]}"
    ).strip()
    vec = generate_embedding(embed_text)

    # 4. Upsert ResumeEmbedding via raw SQL (to use ::vector cast)
    await db.execute(
        text(
            """
            INSERT INTO resume_embeddings (resume_id, model, embedding, created_at)
            VALUES (:rid, :model, CAST(:vec AS vector), NOW())
            ON CONFLICT (resume_id) DO UPDATE SET
                model = EXCLUDED.model,
                embedding = EXCLUDED.embedding,
                created_at = NOW()
            """
        ),
        {"rid": resume.id, "model": "all-MiniLM-L6-v2", "vec": _vec_literal(vec)},
    )
    await db.commit()

    # 5. Trigger AI matching synchronously — runs the cosine query and
    #    refreshes the job_matches table for this user. This is fast (<2s
    #    for 50k jobs once embeddings exist).
    try:
        n_matches = await recompute_matches_for_user(db, current_user.id, top=50)
    except Exception as e:
        # Don't fail the upload if matching fails (e.g. no job embeddings yet)
        n_matches = 0
        import traceback
        print(f"[resumes/upload] matching FAILED: {e}", flush=True)
        traceback.print_exc()

    return {
        "id": resume.id,
        "name": resume.name,
        "target_role": resume.target_role,
        "skills": resume.skills_json,
        "experience_level": resume.experience_level,
        "overall_strength_score": resume.overall_strength_score,
        "is_active": True,
        "matches_computed": n_matches,
    }


@router.get("")
async def list_resumes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ResumeVersion).where(ResumeVersion.user_id == current_user.id)
    )
    resumes = result.scalars().all()
    return [
        {
            "id": r.id, "name": r.name, "target_role": r.target_role,
            "is_active": r.is_active, "experience_level": r.experience_level,
            "overall_strength_score": r.overall_strength_score,
            "created_at": r.created_at.isoformat(),
        }
        for r in resumes
    ]


@router.patch("/{resume_id}/activate")
async def activate_resume(
    resume_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ResumeVersion).where(ResumeVersion.user_id == current_user.id)
    )
    found = False
    for r in result.scalars().all():
        if r.id == resume_id:
            r.is_active = True
            found = True
        else:
            r.is_active = False
    if not found:
        raise HTTPException(status_code=404, detail="Resume not found")
    await db.commit()

    # Re-run matching for the newly active resume
    try:
        n = await recompute_matches_for_user(db, current_user.id, top=50)
    except Exception as e:
        n = 0
        import traceback
        print(f"[resumes/activate] matching FAILED: {e}", flush=True)
        traceback.print_exc()

    return {"message": f"Resume {resume_id} activated", "matches_computed": n}
