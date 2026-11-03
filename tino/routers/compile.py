'''REST endpoints for compiling Typst files to SVG, PDF or DOCX.'''

import faulthandler
import logging
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..dependencies import get_compiler_service, require_viewer
from ..services.compiler import CompileError, CompilerService, PandocMissing

logger = logging.getLogger(__name__)

# Dump all thread stacks to stderr (journald) when gunicorn aborts a hung
# worker (WORKER TIMEOUT sends SIGABRT). Imported after gunicorn installs its
# own signal handlers, so this one wins.
faulthandler.enable(all_threads=True)

DOCX_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

router = APIRouter(prefix='/api/buckets/{slug}/compile', tags=['compile'])

# Sync (not async) handlers: FastAPI runs them in its threadpool, so the
# blocking `typst` subprocess no longer stalls the event loop (which made
# gunicorn kill the worker with WORKER TIMEOUT during collab editing).

# Threadpool handlers would otherwise run every compile at once. Each `typst`
# uses ~320 MB, so 8 together got the worker OOM-killed by the container's 2 GB
# limit. Waiters give up after the timeout to avoid filling the threadpool.
_COMPILE_SLOTS = threading.BoundedSemaphore(2)
_COMPILE_WAIT_SECONDS = 60


@contextmanager
def _compile_slot():
    '''Wait for a free compile slot, or fail with 503 if none frees up in time.'''
    if not _COMPILE_SLOTS.acquire(timeout=_COMPILE_WAIT_SECONDS):  # pylint: disable=consider-using-with
        raise HTTPException(503, 'Too many compiles in progress, try again')

    try:
        yield
    finally:
        _COMPILE_SLOTS.release()


def _compile_failed(exc: CompileError) -> HTTPException:
    '''Build the 422 response for a failed compile.'''
    return HTTPException(422, detail={
        'message': str(exc),
        'diagnostics': exc.diagnostics,
    })


@router.get('/pdf/{path:path}')
def compile_pdf(
    slug: str, path: str,
    user=Depends(require_viewer),
    svc: CompilerService = Depends(get_compiler_service),
):
    '''Compile a .typ file and return the PDF.'''
    logger.info('Compiling PDF for %s/%s (user: %s)', slug, path, user.username)
    try:
        with _compile_slot():
            pdf_path = svc.compile_pdf(slug, path)
        filename = path.rsplit('/', maxsplit=1)[-1].replace('.typ', '.pdf')

        return FileResponse(
            pdf_path,
            filename=filename,
            media_type='application/pdf',
            background=BackgroundTask(pdf_path.unlink, missing_ok=True),
        )

    except FileNotFoundError as exc:
        raise HTTPException(404, 'File not found') from exc

    except CompileError as exc:
        raise _compile_failed(exc) from exc

    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/docx/{path:path}')
def compile_docx(
    slug: str, path: str,
    user=Depends(require_viewer),
    svc: CompilerService = Depends(get_compiler_service),
):
    '''Compile a .typ file and return a DOCX (via Typst's HTML export and pandoc).'''
    logger.info('Compiling DOCX for %s/%s (user: %s)', slug, path, user.username)
    try:
        with _compile_slot():
            docx_path = svc.compile_docx(slug, path)
        filename = path.rsplit('/', maxsplit=1)[-1].replace('.typ', '.docx')

        return FileResponse(
            docx_path,
            filename=filename,
            media_type=DOCX_MEDIA_TYPE,
            background=BackgroundTask(docx_path.unlink, missing_ok=True),
        )

    except FileNotFoundError as exc:
        raise HTTPException(404, 'File not found') from exc

    except PandocMissing as exc:
        raise HTTPException(501, str(exc)) from exc

    except CompileError as exc:
        raise _compile_failed(exc) from exc

    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/svg/{path:path}')
def compile_svg(
    slug: str, path: str,
    user=Depends(require_viewer),
    svc: CompilerService = Depends(get_compiler_service),
):
    '''Compile a .typ file and return a list of SVG strings (one per page).'''
    logger.debug('Compiling SVG for %s/%s (user: %s)', slug, path, user.username)
    try:
        with _compile_slot():
            pages = svc.compile_svg(slug, path)
        return {'pages': pages}

    except FileNotFoundError as exc:
        raise HTTPException(404, 'File not found') from exc

    except CompileError as exc:
        raise _compile_failed(exc) from exc

    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
