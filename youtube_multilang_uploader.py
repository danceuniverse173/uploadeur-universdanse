#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Personal Video Uploader - YouTube multilingue

Structure attendue (créée par split_multilang_export.py) :

    DOSSIER_DATE/
      en_English/
        1_Chanson A/
          Chanson A_en.mp4
          youtube_short.txt
          youtube_short.json
        2_Chanson B/
          ...
      fr_Français/
        1_...
        2_...
      ...

Chaque dossier de langue correspond à UNE chaîne YouTube distincte.

AUTHENTIFICATION
----------------
Le même fichier OAuth Desktop App (client_secrets.json) est utilisé pour
toutes les chaînes, mais le script conserve UN TOKEN PAR LANGUE :

    tokens/en.json
    tokens/fr.json
    tokens/es.json
    ...

Lors de la première autorisation de chaque langue, connectez-vous avec le
compte Google qui gère vos chaînes puis choisissez la chaîne YouTube
correspondant à cette langue lorsque Google/YouTube vous le propose.

SCOPE
-----
Le script demande uniquement :
    https://www.googleapis.com/auth/youtube.upload

Il n'utilise pas channels.list ni d'autres endpoints YouTube Data API.
L'upload est effectué avec youtube.videos.insert.

PROGRAMMATION
-------------
Pour une publication programmée, YouTube exige :
    status.privacyStatus = "private"
    status.publishAt = date/heure ISO 8601

YouTube rend ensuite la vidéo publique à publishAt, sous réserve que le
projet API soit autorisé à publier publiquement.

Exemple :
    python youtube_multilang_uploader.py plan \
        "C:\\Brainpec\\outputs\\2026-08-20" \
        --start-date 2026-08-25 \
        --time 18:00 \
        --timezone Europe/Paris

    python youtube_multilang_uploader.py authorize \
        "C:\\Brainpec\\outputs\\2026-08-20"

    python youtube_multilang_uploader.py upload \
        "C:\\Brainpec\\outputs\\2026-08-20" \
        --start-date 2026-08-25 \
        --time 18:00 \
        --timezone Europe/Paris
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

Request = None
Credentials = None
InstalledAppFlow = None
build = None
HttpError = None
MediaFileUpload = None


def require_google_libraries() -> None:
    """Charge les bibliothèques Google uniquement pour authorize/upload."""
    global Request, Credentials, InstalledAppFlow, build, HttpError, MediaFileUpload
    if all(
        value is not None
        for value in (
            Request,
            Credentials,
            InstalledAppFlow,
            build,
            HttpError,
            MediaFileUpload,
        )
    ):
        return
    try:
        from google.auth.transport.requests import Request as _Request
        from google.oauth2.credentials import Credentials as _Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow as _InstalledAppFlow
        from googleapiclient.discovery import build as _build
        from googleapiclient.errors import HttpError as _HttpError
        from googleapiclient.http import MediaFileUpload as _MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "Dépendances Google manquantes. Installez-les avec : "
            "pip install google-api-python-client google-auth-oauthlib "
            "google-auth-httplib2"
        ) from exc

    Request = _Request
    Credentials = _Credentials
    InstalledAppFlow = _InstalledAppFlow
    build = _build
    HttpError = _HttpError
    MediaFileUpload = _MediaFileUpload


BUILD = "2026.08.17-youtube-multilang-scheduler-v1"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".webm", ".mkv"}

MAX_RETRIES = 10
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (
    OSError,
    IOError,
    socket.timeout,
    ConnectionError,
)


@dataclass
class UploadItem:
    language_code: str
    language_folder: str
    song_folder: str
    order: int
    video_path: str
    metadata_path: str | None
    title: str
    description: str
    publish_local: str
    publish_utc: str


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def clean_language_code(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "_", value)
    return value.strip("_") or "xx"


def folder_numeric_prefix(path: Path) -> int:
    match = re.match(r"^\s*(\d+)[_\-\s]", path.name)
    if match:
        return int(match.group(1))
    return 10**9


def find_video(song_dir: Path) -> Path:
    videos = sorted(
        p for p in song_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if len(videos) != 1:
        raise RuntimeError(
            f"{song_dir}: attendu exactement 1 vidéo, trouvé {len(videos)}."
        )
    return videos[0]


def parse_youtube_short_txt(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")

    def extract_section(heading: str, next_heading: str | None = None) -> str:
        start_match = re.search(
            rf"(?mi)^{re.escape(heading)}\s*$\n^-+\s*$\n",
            text,
        )
        if not start_match:
            return ""
        start = start_match.end()
        if next_heading:
            end_match = re.search(
                rf"(?mi)^\s*{re.escape(next_heading)}\s*$\n^-+\s*$",
                text[start:],
            )
            if end_match:
                return text[start:start + end_match.start()].strip()
        return text[start:].strip()

    title = extract_section("TITRE YOUTUBE", "DESCRIPTION YOUTUBE")
    description = extract_section("DESCRIPTION YOUTUBE", "HEURE DE PUBLICATION")

    language_code = ""
    language_name = ""
    m = re.search(r"(?mi)^Langue\s*:\s*(.*?)\s*\[([^\]]+)\]\s*$", text)
    if m:
        language_name = m.group(1).strip()
        language_code = m.group(2).strip()

    return {
        "youtube_title": title,
        "description": description,
        "language_code": language_code,
        "language_name": language_name,
    }


def load_metadata(song_dir: Path) -> tuple[dict[str, Any], Path | None]:
    json_path = song_dir / "youtube_short.json"
    txt_path = song_dir / "youtube_short.txt"

    if json_path.is_file():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return data, json_path

    if txt_path.is_file():
        return parse_youtube_short_txt(txt_path), txt_path

    raise RuntimeError(
        f"{song_dir}: youtube_short.json ou youtube_short.txt introuvable."
    )


def infer_language_code(language_dir: Path, metadata: dict[str, Any]) -> str:
    code = str(metadata.get("language_code") or "").strip()
    if code:
        return clean_language_code(code)

    # split_multilang_export.py crée généralement des dossiers comme en_English.
    first = re.split(r"[_\-\s]+", language_dir.name, maxsplit=1)[0]
    return clean_language_code(first)


def discover_items(root: Path) -> list[tuple[Path, Path, Path, dict[str, Any], str]]:
    """
    Retourne :
      (language_dir, song_dir, video_path, metadata, language_code)
    """
    discovered: list[tuple[Path, Path, Path, dict[str, Any], str]] = []

    language_dirs = sorted(
        p for p in root.iterdir()
        if p.is_dir() and p.name not in {"tokens", ".tokens", "__pycache__"}
    )

    for language_dir in language_dirs:
        song_dirs = sorted(
            (p for p in language_dir.iterdir() if p.is_dir()),
            key=lambda p: (folder_numeric_prefix(p), p.name.casefold()),
        )
        for song_dir in song_dirs:
            try:
                metadata, _metadata_path = load_metadata(song_dir)
                video_path = find_video(song_dir)
            except RuntimeError:
                # Ignore un sous-dossier qui n'a pas la structure d'une vidéo exportée.
                continue

            language_code = infer_language_code(language_dir, metadata)
            discovered.append(
                (language_dir, song_dir, video_path, metadata, language_code)
            )

    if not discovered:
        raise RuntimeError(
            f"Aucune vidéo exportée détectée dans : {root}"
        )
    return discovered


def parse_clock(value: str) -> dt_time:
    try:
        hour_s, minute_s = value.strip().split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
        return dt_time(hour=hour, minute=minute)
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            "L'heure doit utiliser HH:MM, par exemple 18:30."
        ) from exc


def build_schedule(
    root: Path,
    *,
    start_date: date,
    clock: dt_time,
    timezone_name: str,
) -> list[UploadItem]:
    tz = ZoneInfo(timezone_name)
    discovered = discover_items(root)

    by_language: dict[str, list[tuple[Path, Path, Path, dict[str, Any], str]]] = {}
    for row in discovered:
        by_language.setdefault(row[4], []).append(row)

    plan: list[UploadItem] = []

    for language_code in sorted(by_language):
        rows = sorted(
            by_language[language_code],
            key=lambda row: (
                folder_numeric_prefix(row[1]),
                row[1].name.casefold(),
            ),
        )

        for index, (language_dir, song_dir, video_path, metadata, _code) in enumerate(rows):
            publish_day = start_date + timedelta(days=index)
            local_dt = datetime.combine(publish_day, clock, tzinfo=tz)

            if local_dt <= datetime.now(tz):
                raise RuntimeError(
                    f"La programmation de {video_path.name} tomberait dans le passé : "
                    f"{local_dt.isoformat()}."
                )

            utc_dt = local_dt.astimezone(timezone.utc)

            title = str(
                metadata.get("youtube_title")
                or metadata.get("title")
                or ""
            ).strip()
            description = str(metadata.get("description") or "").strip()

            if not title:
                raise RuntimeError(
                    f"{song_dir}: aucun titre YouTube trouvé dans les métadonnées."
                )

            metadata_path = None
            if (song_dir / "youtube_short.json").is_file():
                metadata_path = str(song_dir / "youtube_short.json")
            elif (song_dir / "youtube_short.txt").is_file():
                metadata_path = str(song_dir / "youtube_short.txt")

            plan.append(
                UploadItem(
                    language_code=language_code,
                    language_folder=language_dir.name,
                    song_folder=song_dir.name,
                    order=index + 1,
                    video_path=str(video_path),
                    metadata_path=metadata_path,
                    title=title[:100],
                    description=description,
                    publish_local=local_dt.isoformat(),
                    publish_utc=utc_dt.isoformat().replace("+00:00", "Z"),
                )
            )

    return plan


def client_secrets_path(args: argparse.Namespace) -> Path:
    path = Path(args.client_secrets).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(
            f"Fichier OAuth introuvable : {path}\n"
            "Téléchargez le JSON du client OAuth 2.0 'Application de bureau' "
            "depuis Google Cloud et utilisez --client-secrets."
        )
    return path


def token_path(root: Path, language_code: str) -> Path:
    return root / "tokens" / f"{clean_language_code(language_code)}.json"


def authorize_language(
    *,
    root: Path,
    language_code: str,
    secrets: Path,
    force: bool,
) -> Path:
    require_google_libraries()
    path = token_path(root, language_code)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        print(f"✅ [{language_code}] token déjà présent : {path}")
        return path

    if force and path.exists():
        path.unlink()

    print()
    print("=" * 78)
    print(f"AUTHENTIFICATION DE LA CHAÎNE POUR LA LANGUE [{language_code}]")
    print("=" * 78)
    print(
        "Le navigateur va s'ouvrir.\n"
        "Connectez-vous avec votre compte Google puis choisissez LA CHAÎNE YOUTUBE\n"
        f"qui doit recevoir les vidéos de la langue [{language_code}].\n"
        "Le token obtenu sera stocké séparément pour cette langue."
    )
    input("Appuyez sur Entrée pour continuer... ")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(secrets),
        SCOPES,
    )

    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        authorization_prompt_message=(
            f"Autorisation YouTube pour la langue [{language_code}]. "
            "Votre navigateur va s'ouvrir : {url}"
        ),
        success_message=(
            f"Autorisation terminée pour [{language_code}]. "
            "Vous pouvez fermer cette fenêtre."
        ),
        open_browser=True,
        access_type="offline",
        prompt="consent",
    )

    path.write_text(credentials.to_json() + "\n", encoding="utf-8")
    print(f"✅ [{language_code}] token enregistré : {path}")
    return path


def load_credentials(root: Path, language_code: str):
    require_google_libraries()
    path = token_path(root, language_code)
    if not path.is_file():
        raise RuntimeError(
            f"Token manquant pour [{language_code}] : {path}\n"
            f"Lancez d'abord : authorize --language {language_code}"
        )

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json() + "\n", encoding="utf-8")

    if not creds.valid:
        raise RuntimeError(
            f"Le token OAuth [{language_code}] n'est plus valide. "
            "Relancez authorize avec --force."
        )

    return creds


def youtube_service(credentials):
    require_google_libraries()
    return build(
        API_SERVICE_NAME,
        API_VERSION,
        credentials=credentials,
        cache_discovery=False,
    )


def upload_resumable(
    youtube,
    *,
    item: UploadItem,
    category_id: str,
    made_for_kids: bool,
) -> dict[str, Any]:
    """
    Une vidéo programmée est envoyée en PRIVATE avec publishAt.
    YouTube la rend publique automatiquement à la date prévue.
    """
    require_google_libraries()

    body = {
        "snippet": {
            "title": item.title,
            "description": item.description,
            "categoryId": str(category_id),
            "defaultLanguage": item.language_code,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": item.publish_utc,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }

    media = MediaFileUpload(
        item.video_path,
        chunksize=8 * 1024 * 1024,
        resumable=True,
        mimetype="video/mp4",
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )

    response = None
    retry = 0

    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"      upload : {pct:3d}%")
        except HttpError as exc:
            code = getattr(exc.resp, "status", None)
            if code not in RETRIABLE_STATUS_CODES or retry >= MAX_RETRIES:
                raise
            retry += 1
            sleep_s = min(60, (2 ** retry) + random.random())
            print(
                f"      ⚠️ HTTP {code}, nouvelle tentative dans {sleep_s:.1f}s "
                f"({retry}/{MAX_RETRIES})"
            )
            time.sleep(sleep_s)
        except RETRIABLE_EXCEPTIONS as exc:
            if retry >= MAX_RETRIES:
                raise
            retry += 1
            sleep_s = min(60, (2 ** retry) + random.random())
            print(
                f"      ⚠️ {type(exc).__name__}, nouvelle tentative dans "
                f"{sleep_s:.1f}s ({retry}/{MAX_RETRIES})"
            )
            time.sleep(sleep_s)

    if not isinstance(response, dict) or not response.get("id"):
        raise RuntimeError(f"Réponse YouTube inattendue : {response!r}")

    return response


def state_file(root: Path) -> Path:
    return root / "youtube_upload_state.json"


def load_state(root: Path) -> dict[str, Any]:
    path = state_file(root)
    if not path.is_file():
        return {"build": BUILD, "uploads": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("uploads"), dict):
        data["uploads"] = {}
    return data


def state_key(item: UploadItem) -> str:
    # La combinaison langue + chemin relatif logique permet d'éviter un double upload
    # lors d'une relance du script.
    return f"{item.language_code}|{Path(item.video_path).resolve()}"


def cmd_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    plan = build_schedule(
        root,
        start_date=date.fromisoformat(args.start_date),
        clock=parse_clock(args.time),
        timezone_name=args.timezone,
    )

    print(f"Personal Video Uploader - {BUILD}")
    print(f"Dossier : {root}")
    print()

    current_lang = None
    for item in plan:
        if item.language_code != current_lang:
            current_lang = item.language_code
            print()
            print(f"[{current_lang}] {item.language_folder}")
        print(
            f"  {item.order:02d}. {Path(item.video_path).name}\n"
            f"      publication locale : {item.publish_local}\n"
            f"      publication UTC    : {item.publish_utc}\n"
            f"      titre              : {item.title}"
        )

    plan_path = root / "youtube_upload_plan.json"
    atomic_write_json(
        plan_path,
        {
            "build": BUILD,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "root": str(root),
            "start_date": args.start_date,
            "time": args.time,
            "timezone": args.timezone,
            "items": [asdict(item) for item in plan],
        },
    )
    print(f"\n✅ Plan enregistré : {plan_path}")
    return 0


def cmd_authorize(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    secrets = client_secrets_path(args)

    discovered = discover_items(root)
    languages = sorted({row[4] for row in discovered})

    if args.language:
        requested = clean_language_code(args.language)
        if requested not in languages:
            raise RuntimeError(
                f"Langue [{requested}] absente du dossier. "
                f"Langues détectées : {', '.join(languages)}"
            )
        languages = [requested]

    print(f"Langues à autoriser : {', '.join(languages)}")
    print(
        "\nIMPORTANT : chaque langue correspond à une chaîne différente. "
        "Choisissez la bonne chaîne à chaque ouverture du navigateur."
    )

    for language_code in languages:
        authorize_language(
            root=root,
            language_code=language_code,
            secrets=secrets,
            force=args.force,
        )

    print("\n✅ Authentification terminée.")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()

    plan = build_schedule(
        root,
        start_date=date.fromisoformat(args.start_date),
        clock=parse_clock(args.time),
        timezone_name=args.timezone,
    )

    if args.language:
        requested = clean_language_code(args.language)
        plan = [item for item in plan if item.language_code == requested]
        if not plan:
            raise RuntimeError(f"Aucune vidéo pour [{requested}].")

    state = load_state(root)
    uploads = state["uploads"]

    pending = [
        item for item in plan
        if args.reupload or state_key(item) not in uploads
    ]

    print(f"Personal Video Uploader - {BUILD}")
    print(f"Vidéos dans le plan : {len(plan)}")
    print(f"Déjà envoyées       : {len(plan) - len(pending)}")
    print(f"À envoyer           : {len(pending)}")
    print()

    if not pending:
        print("✅ Rien à envoyer.")
        return 0

    if not args.yes:
        print("PROGRAMMATION PRÉVUE :")
        for item in pending:
            print(
                f"  [{item.language_code}] {Path(item.video_path).name} "
                f"-> {item.publish_local}"
            )
        print()
        answer = input(
            "Continuer et envoyer réellement ces vidéos à YouTube ? "
            "Tapez OUI : "
        ).strip()
        if answer != "OUI":
            print("Annulé.")
            return 2

    services: dict[str, Any] = {}

    for index, item in enumerate(pending, start=1):
        print()
        print(
            f"[{index}/{len(pending)}] [{item.language_code}] "
            f"{Path(item.video_path).name}"
        )
        print(f"      programmé : {item.publish_local}")

        if item.language_code not in services:
            creds = load_credentials(root, item.language_code)
            services[item.language_code] = youtube_service(creds)

        try:
            response = upload_resumable(
                services[item.language_code],
                item=item,
                category_id=args.category_id,
                made_for_kids=args.made_for_kids,
            )
        except Exception as exc:
            state.setdefault("errors", []).append(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "language_code": item.language_code,
                    "video_path": item.video_path,
                    "publish_utc": item.publish_utc,
                    "error": repr(exc),
                }
            )
            atomic_write_json(state_file(root), state)
            raise

        video_id = str(response["id"])
        uploads[state_key(item)] = {
            "video_id": video_id,
            "language_code": item.language_code,
            "video_path": item.video_path,
            "title": item.title,
            "publish_local": item.publish_local,
            "publish_utc": item.publish_utc,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "requested_privacy_status": "private",
            "scheduled_publication": True,
        }
        state["build"] = BUILD
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(state_file(root), state)

        print(f"      ✅ upload terminé : https://youtu.be/{video_id}")

    print()
    print("✅ Tous les uploads demandés sont terminés.")
    print(f"État : {state_file(root)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Upload et programmation quotidienne des exports multilingues "
            "créés par split_multilang_export.py."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common_root = argparse.ArgumentParser(add_help=False)
    common_root.add_argument(
        "root",
        help="Dossier DATE contenant les sous-dossiers de langues.",
    )

    p_plan = sub.add_parser(
        "plan",
        parents=[common_root],
        help="Affiche et enregistre le calendrier sans appeler YouTube.",
    )
    p_plan.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p_plan.add_argument("--time", required=True, help="HH:MM")
    p_plan.add_argument("--timezone", default="Europe/Paris")
    p_plan.set_defaults(func=cmd_plan)

    p_auth = sub.add_parser(
        "authorize",
        parents=[common_root],
        help="Crée un token OAuth distinct pour chaque chaîne/langue.",
    )
    p_auth.add_argument(
        "--client-secrets",
        default="client_secrets.json",
        help="JSON OAuth Desktop App téléchargé depuis Google Cloud.",
    )
    p_auth.add_argument(
        "--language",
        help="N'autoriser qu'une langue (ex: en). Sans option : toutes.",
    )
    p_auth.add_argument(
        "--force",
        action="store_true",
        help="Supprime et recrée le token de la langue.",
    )
    p_auth.set_defaults(func=cmd_authorize)

    p_upload = sub.add_parser(
        "upload",
        parents=[common_root],
        help="Upload réellement les vidéos et programme leur publication.",
    )
    p_upload.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p_upload.add_argument("--time", required=True, help="HH:MM")
    p_upload.add_argument("--timezone", default="Europe/Paris")
    p_upload.add_argument("--language", help="Limiter l'upload à une langue.")
    p_upload.add_argument(
        "--category-id",
        default="24",
        help="Catégorie YouTube. 24 = Entertainment.",
    )
    p_upload.add_argument(
        "--made-for-kids",
        action="store_true",
        help="Déclarer les vidéos comme conçues pour les enfants.",
    )
    p_upload.add_argument(
        "--yes",
        action="store_true",
        help="Ne pas demander la confirmation OUI avant l'upload.",
    )
    p_upload.add_argument(
        "--reupload",
        action="store_true",
        help="Autoriser un nouvel upload même si l'état indique déjà un upload.",
    )
    p_upload.set_defaults(func=cmd_upload)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        eprint("\nInterrompu.")
        return 130
    except Exception as exc:
        eprint(f"\n❌ {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
