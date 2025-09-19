#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Music Covers Auto Setup (YouTube-friendly v2)

Mejoras vs versión anterior:
- Búsqueda en iTunes en varios países (ES, US, BR) para aumentar hits.
- Limpieza de títulos más agresiva (emojis, tags repetidos, separadores).
- Fallback: si no hay arte en iTunes/MusicBrainz, intenta usar la miniatura de YouTube
  detectando el ID (maxresdefault.jpg, hqdefault.jpg).
- Mantiene: auto-instalación de deps, diálogo de carpeta, sidecar/incrustado, refresh Jellyfin.

Uso:
    python music_covers_auto_setup_youtube_v2.py
"""

import io
import os
import re
import sys
import json
import time
import subprocess
import threading
import logging
import concurrent.futures
from pathlib import Path
from typing import Optional, Tuple

# ------------- Instalación de dependencias -------------

REQUIRED = ["mutagen", "requests", "Pillow"]
def ensure_deps():
    missing = []
    for pkg in REQUIRED:
        try:
            __import__(pkg if pkg != "Pillow" else "PIL")
        except Exception:
            missing.append(pkg)
    if not missing:
        return
    print(f"Instalando dependencias: {', '.join(missing)} ...")
    py = sys.executable
    cmd = [py, "-m", "pip", "install", "--upgrade"] + missing
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        print("ERROR instalando dependencias. Ejecutá manualmente:", " ".join(cmd), file=sys.stderr)
        sys.exit(1)

ensure_deps()

# Ahora que están, importamos
import requests
from PIL import Image
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

# ------------- Utilidades UI -------------
def ask_directory_gui() -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Selecciona la carpeta de MÚSICA")
        root.destroy()
        return path if path else None
    except Exception:
        return None

def ask_file_gui(title="Selecciona una imagen JPG/PNG") -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title=title, filetypes=[("Imágenes", "*.jpg;*.jpeg;*.png")])
        root.destroy()
        return path if path else None
    except Exception:
        return None

def yes_no(prompt: str, default: bool=False) -> bool:
    d = "S/n" if default else "s/N"
    while True:
        ans = input(f"{prompt} [{d}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("s", "si", "sí", "y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("Respuesta no válida. Escribí s/n.")

def input_int(prompt: str, default: int) -> int:
    while True:
        ans = input(f"{prompt} [{default}]: ").strip()
        if not ans:
            return default
        try:
            return int(ans)
        except ValueError:
            print("Poné un número.")

def cpu_count_default() -> int:
    try:
        import multiprocessing as mp
        n = mp.cpu_count()
        return max(2, min(8, n))
    except Exception:
        return 4

# ------------- Limpieza de títulos estilo YouTube -------------

YTB_NOISE_WORDS = r"(official|video oficial|official video|official audio|audio|lyrics?|letra|remix|live|en vivo|karaoke|sped up|slowed(?:\s+and\s+reverb)?|reverb|extended|full(?: version)?|color coded|visualizer|audio lyrics|sub(?:títulos)?(?: en español)?)"

YTB_NOISE_PATTERNS = [
    rf"\(({YTB_NOISE_WORDS})\)",
    rf"\[({YTB_NOISE_WORDS})\]",
    rf"\b{YTB_NOISE_WORDS}\b",
    r"\b(HD|4K|8K)\b",
    r"\s*\|\s*.*$",  # corta a partir de " | "
    r"\s*—\s*.*$",  # corta a partir de em-dash variantes
    r"\s*–\s*.*$",
]

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+", flags=re.UNICODE
)

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def clean_youtube_title(text: str) -> str:
    s = text
    # quitar emojis
    s = EMOJI_PATTERN.sub("", s)
    # borrar " - Topic" al final
    s = re.sub(r"\s*-\s*Topic$", "", s, flags=re.I)
    # limpiar comillas y guiones típicos
    s = s.replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    # quitar numeración inicial
    s = re.sub(r"^\s*\d+\s*[-_.]\s*", "", s)
    # eliminar contenido entre paréntesis, corchetes, llaves con palabras ruidosas
    for pat in YTB_NOISE_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    # eliminar restos dentro de ()/[]/{} si quedan frases largas
    s = re.sub(r"\([^)]{1,40}\)", "", s)
    s = re.sub(r"\[[^\]]{1,40}\]", "", s)
    s = re.sub(r"\{[^}]{1,40}\}", "", s)
    # normalizar "feat/ft"
    s = re.sub(r"\s+(feat\.?|ft\.)\s+", " feat. ", s, flags=re.I)
    # colapsar múltiples guiones
    s = re.sub(r"\s*-\s*", " - ", s)
    # espacios
    s = normalize_spaces(s)
    return s

def split_artist_title(clean_name: str) -> Tuple[Optional[str], Optional[str]]:
    # Preferir "Artista - Título"
    if " - " in clean_name:
        artist, title = clean_name.split(" - ", 1)
        return normalize_spaces(artist), normalize_spaces(title)
    # "Artista: Título"
    if ":" in clean_name:
        parts = clean_name.split(":", 1)
        return normalize_spaces(parts[0]), normalize_spaces(parts[1])
    return None, clean_name  # sin artista

# ------------- YouTube helpers -------------
# Detectar un ID de YouTube en el nombre del archivo (muchos descargadores lo incluyen)
YOUTUBE_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")

def extract_youtube_id(text: str) -> Optional[str]:
    # Buscar patrones comunes de ID o URL
    # URLs típicos:
    #   https://www.youtube.com/watch?v=VIDEOID
    #   https://youtu.be/VIDEOID
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    # Por si solo quedó el ID en el nombre
    m = YOUTUBE_ID_RE.search(text)
    if m:
        return m.group(1)
    return None

def fetch_youtube_thumbnail(video_id: str) -> Optional[bytes]:
    # Probar maxresdefault luego hqdefault
    bases = [
        f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    ]
    for url in bases:
        try:
            r = requests.get(url, timeout=12)
            if r.status_code == 200 and r.content and len(r.content) > 5000:  # evitar imagenes vacías
                return r.content
        except Exception:
            pass
    return None

# ------------- Búsqueda e incrustación de portadas -------------

ITUNES_SEARCH = "https://itunes.apple.com/search"
MB_RECORDING_SEARCH = "https://musicbrainz.org/ws/2/recording"
CAA_RELEASE_FRONT = "https://coverartarchive.org/release/{mbid}/front-1200"

HEADERS = {
    "User-Agent": "AutoCoverEmbedder/2.0-yt (contact: user@example.com)"
}

print_lock = threading.Lock()
def log(msg: str):
    with print_lock:
        print(msg, flush=True)

def read_tags_or_filename(path: Path) -> Tuple[Optional[str], Optional[str], bool, Optional[str]]:
    """
    Lee tags; si faltan, usa nombre de archivo (limpio).
    Devuelve (artist, title, has_art, youtube_id)
    """
    artist = title = None
    has_art = False
    ytid = None
    try:
        audio = MutagenFile(path)
        if audio is not None:
            if isinstance(audio, FLAC):
                artist = (audio.get("artist") or audio.get("ARTIST") or [None])[0]
                title  = (audio.get("title")  or audio.get("TITLE")  or [None])[0]
                has_art = bool(getattr(audio, "pictures", []))
            elif isinstance(audio, MP4):
                tags = audio.tags or {}
                artist = (tags.get("\xa9ART") or [None])[0]
                title  = (tags.get("\xa9nam") or [None])[0]
                has_art = "covr" in tags and bool(tags["covr"])
            else:
                if hasattr(audio, "tags") and audio.tags:
                    a = audio.tags.get("TPE1")
                    if a:
                        artist = str(a.text[0]) if hasattr(a, "text") else str(a)
                    t = audio.tags.get("TIT2")
                    if t:
                        title = str(t.text[0]) if hasattr(t, "text") else str(t)
                    try:
                        id3 = ID3(path)
                        has_art = any(k.startswith("APIC") for k in id3.keys())
                    except ID3NoHeaderError:
                        has_art = False
    except Exception:
        pass

    base = path.stem
    ytid = extract_youtube_id(base)

    # Si faltan, usar el nombre del archivo limpiando adornos YouTube
    if not artist or not title:
        base_clean = clean_youtube_title(base)
        a2, t2 = split_artist_title(base_clean)
        artist = artist or a2
        title = title or t2

    if artist:
        artist = normalize_spaces(artist)
    if title:
        title = normalize_spaces(title)

    return artist, title, has_art, ytid

def resize_to_jpeg(img_bytes: bytes, max_size: int):
    im = Image.open(io.BytesIO(img_bytes))
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > max_size:
        if w >= h:
            new_w = max_size
            new_h = int(h * (max_size / w))
        else:
            new_h = max_size
            new_w = int(w * (max_size / h))
        im = im.resize((new_w, new_h), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue(), "image/jpeg"

def fetch_itunes(artist: Optional[str], title: str, size_px: int=1200):
    term = f"{artist} {title}".strip() if artist else title
    # probar en varios países para aumentar el hit rate
    for cc in ("ES", "US", "BR", "MX", "AR"):
        params = {"term": term, "media": "music", "entity": "song", "limit": 10, "country": cc}
        try:
            r = requests.get(ITUNES_SEARCH, params=params, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            ct = title.lower()
            # 1) preferir coincidencias por título
            for item in data.get("results", []):
                name = (item.get("trackName") or "").lower()
                if name and (name == ct or ct in name or name in ct):
                    art_url = item.get("artworkUrl100") or item.get("artworkUrl60")
                    if art_url:
                        art_url = re.sub(r"/\d+x\d+bb\.(jpg|png)$", f"/{size_px}x{size_px}bb.\\1", art_url)
                        img = requests.get(art_url, headers=HEADERS, timeout=15)
                        if img.status_code == 200 and img.content:
                            return img.content
            # 2) si no, el primero que tenga arte
            for item in data.get("results", []):
                art_url = item.get("artworkUrl100") or item.get("artworkUrl60")
                if art_url:
                    art_url = re.sub(r"/\d+x\d+bb\.(jpg|png)$", f"/{size_px}x{size_px}bb.\\1", art_url)
                    img = requests.get(art_url, headers=HEADERS, timeout=15)
                    if img.status_code == 200 and img.content:
                        return img.content
        except Exception:
            continue
    return None

def fetch_musicbrainz(artist: Optional[str], title: str):
    query = f'recording:"{title}" AND artist:"{artist}"' if artist else f'recording:"{title}"'
    params = {"query": query, "fmt": "json", "limit": 5}
    try:
        r = requests.get(MB_RECORDING_SEARCH, params=params, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        for rec in data.get("recordings", []):
            for rel in rec.get("releases", []):
                mbid = rel.get("id")
                if not mbid:
                    continue
                time.sleep(1.0)
                url = CAA_RELEASE_FRONT.format(mbid=mbid)
                img = requests.get(url, headers=HEADERS, timeout=20)
                if img.status_code == 200 and img.content:
                    return img.content
    except Exception:
        return None
    return None

def find_art(artist: Optional[str], title: str, yt_id: Optional[str], max_size: int):
    # 1) iTunes
    img = fetch_itunes(artist, title, size_px=max_size)
    if img:
        return resize_to_jpeg(img, max_size)
    # 2) MusicBrainz + CAA
    img = fetch_musicbrainz(artist, title)
    if img:
        return resize_to_jpeg(img, max_size)
    # 3) YouTube thumbnail fallback si tenemos ID
    if yt_id:
        img = fetch_youtube_thumbnail(yt_id)
        if img:
            return resize_to_jpeg(img, max_size)
    return None

def embed_mp3(path: Path, img_bytes: bytes, mime: str):
    try:
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            id3 = ID3()
        for k in list(id3.keys()):
            if k.startswith("APIC"):
                del id3[k]
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img_bytes))
        id3.save(path)
    except Exception as e:
        raise RuntimeError(f"MP3 embed falló: {e}")

def embed_flac(path: Path, img_bytes: bytes, mime: str):
    try:
        f = FLAC(path)
        f.clear_pictures()
        pic = Picture()
        pic.data = img_bytes
        pic.type = 3
        pic.desc = "Cover"
        pic.mime = mime
        try:
            im = Image.open(io.BytesIO(img_bytes))
            pic.width, pic.height = im.size
            pic.depth = 24
        except Exception:
            pass
        f.add_picture(pic)
        f.save()
    except Exception as e:
        raise RuntimeError(f"FLAC embed falló: {e}")

def embed_m4a(path: Path, img_bytes: bytes):
    try:
        mp4 = MP4(path)
        mp4.tags["covr"] = [MP4Cover(img_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        mp4.save()
    except Exception as e:
        raise RuntimeError(f"M4A embed falló: {e}")

def save_sidecar(path: Path, img_bytes: bytes):
    out = path.with_suffix(".jpg")
    with open(out, "wb") as f:
        f.write(img_bytes)
    return out

def process_file(path: Path, cfg):
    artist, title, has_art, yt_id = read_tags_or_filename(path)
    if cfg["skip_if_has_art"] and has_art and not cfg["force"]:
        return "skip", f"{path.name}: ya tenía portada"

    if not title:
        return "skip", f"{path.name}: no pude deducir título"

    art = find_art(artist, title, yt_id, cfg["max_size"])
    if not art:
        if cfg["placeholder_path"]:
            try:
                with open(cfg["placeholder_path"], "rb") as ph:
                    img_bytes = ph.read()
                out = save_sidecar(path, img_bytes)
                return "sidecar", f"{path.name}: placeholder {out.name}"
            except Exception as e:
                return "error", f"{path.name}: placeholder falló: {e}"
        return "notfound", f"{path.name}: sin arte online (artist={artist or 'desconocido'}; title={title}; yt={yt_id or 'no'})"

    img_bytes, mime = art
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            embed_mp3(path, img_bytes, mime)
            return "ok", f"{path.name}: embebido mp3"
        elif ext == ".flac":
            embed_flac(path, img_bytes, mime)
            return "ok", f"{path.name}: embebido flac"
        elif ext in (".m4a", ".mp4", ".aac"):
            embed_m4a(path, img_bytes)
            return "ok", f"{path.name}: embebido m4a"
        elif ext in (".ogg", ".opus"):
            if cfg["try_ogg_embed"]:
                try:
                    import base64
                    from mutagen.oggvorbis import OggVorbis
                    ov = OggVorbis(path)
                    pic = Picture()
                    pic.data = img_bytes
                    pic.type = 3
                    pic.desc = "Cover"
                    pic.mime = mime
                    b = pic.write()
                    ov["METADATA_BLOCK_PICTURE"] = [base64.b64encode(b).decode("ascii")]
                    ov.save()
                    return "ok", f"{path.name}: embebido ogg (experimental)"
                except Exception:
                    out = save_sidecar(path, img_bytes)
                    return "sidecar", f"{path.name}: sidecar {out.name} (ogg embed falló)"
            else:
                out = save_sidecar(path, img_bytes)
                return "sidecar", f"{path.name}: sidecar {out.name}"
        else:
            if cfg["sidecar"]:
                out = save_sidecar(path, img_bytes)
                return "sidecar", f"{path.name}: sidecar {out.name}"
            return "unsupported", f"{path.name}: formato no soportado (usa sidecar)"
    except Exception as e:
        return "error", f"{path.name}: {e}"

def iter_audio(root: Path):
    exts = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav"}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            yield p

# ------------- Jellyfin API -------------
def jellyfin_refresh(base_url: str, api_key: str):
    url = base_url.rstrip("/") + "/Library/Refresh"
    try:
        r = requests.post(url, headers={"X-Emby-Token": api_key}, timeout=15)
        if r.status_code in (200, 204):
            log("Jellyfin: refresh solicitado.")
            return True
        else:
            log(f"Jellyfin: respuesta {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log(f"Jellyfin: error al refrescar → {e}")
        return False

# ------------- Main Flow -------------

def main():
    print("\n=== Music Covers Auto Setup (YouTube v2) ===\n")

    # Elegir carpeta
    music_dir = ask_directory_gui()
    if not music_dir:
        music_dir = input("Poné la ruta de la carpeta de MÚSICA: ").strip().strip('"')
    root = Path(music_dir).expanduser().resolve()
    if not root.exists():
        print(f"Ruta no encontrada: {root}", file=sys.stderr)
        sys.exit(1)

    # Opciones
    max_size = input_int("Tamaño máximo de la portada (px)", 1200)
    concurrency = input_int("Número de hilos (concurrency)", cpu_count_default())
    sidecar = yes_no("Guardar JPG al lado si no se puede incrustar", True)
    try_ogg_embed = yes_no("Intentar incrustar en OGG/OPUS (experimental)", False)
    force = yes_no("Forzar re-embed aunque ya tengan portada", False)
    skip_if_has_art = not force

    placeholder_path = None
    if yes_no("¿Usar imagen placeholder si no se encuentra arte?", False):
        placeholder_path = ask_file_gui("Selecciona un placeholder (JPG/PNG)")
        if not placeholder_path:
            placeholder_path = input("Ruta del placeholder (o Enter para saltar): ").strip() or None
            if placeholder_path and not Path(placeholder_path).exists():
                print("Placeholder no encontrado. Se ignora.")
                placeholder_path = None

    print("\n--- Configuración ---")
    print("Carpeta:", root)
    print("max_size:", max_size)
    print("concurrency:", concurrency)
    print("sidecar:", sidecar)
    print("try_ogg_embed:", try_ogg_embed)
    print("force:", force)
    print("placeholder:", placeholder_path if placeholder_path else "no")
    print("---------------------\n")

    files = list(iter_audio(root))
    if not files:
        print("No se encontraron archivos de audio.", file=sys.stderr)
        sys.exit(2)

    print(f"Escaneando {len(files)} archivos...\n")
    cfg = {
        "max_size": max_size,
        "sidecar": sidecar,
        "try_ogg_embed": try_ogg_embed,
        "force": force,
        "skip_if_has_art": skip_if_has_art,
        "placeholder_path": placeholder_path,
    }

    statuses = []
    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(process_file, p, cfg) for p in files]
        for fut in concurrent.futures.as_completed(futs):
            status, msg = fut.result()
            statuses.append(status)
            log(f"[{status}] {msg}")

    total = len(statuses)
    summary = {
        "ok": statuses.count("ok"),
        "sidecar": statuses.count("sidecar"),
        "skip": statuses.count("skip"),
        "notfound": statuses.count("notfound"),
        "unsupported": statuses.count("unsupported"),
        "error": statuses.count("error"),
        "elapsed_sec": round(time.time() - start, 1),
        "total": total,
    }

    print("\n=== RESUMEN ===")
    for k in ["ok", "sidecar", "skip", "notfound", "unsupported", "error", "elapsed_sec", "total"]:
        print(f"{k}: {summary[k]}")

    # Refresh Jellyfin opcional
    print()
    if yes_no("¿Quieres solicitar un refresco de la biblioteca en Jellyfin ahora?", False):
        base_url = input("URL base de Jellyfin (ej: http://192.168.1.50:8096): ").strip()
        api_key = input("API Key de Jellyfin: ").strip()
        if base_url and api_key:
            jellyfin_refresh(base_url, api_key)
        else:
            print("No se proporcionaron datos suficientes para el refresco.")

    print("\nListo. Si no ves portadas todavía, en Jellyfin hacé 'Actualizar metadatos → Forzar refresco'.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
