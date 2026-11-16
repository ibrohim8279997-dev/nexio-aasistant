"""Озвучка: edge-tts, голос по языку ТЕКСТА ОТВЕТА, короткие ответы для звонка, ffmpeg для wav 16 кГц и ogg opus.

Для звонка нужен PCM 16 кГц моно, для голосовых сообщений Telegram — ogg opus. Звук хранится только в памяти."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import unescape

from bot.voice.stack import RATE

log = logging.getLogger("voice.tts")

VOICES = {
    "ru": {"svetlana": "ru-RU-SvetlanaNeural", "dmitry": "ru-RU-DmitryNeural"},
    "uz": {"madina": "uz-UZ-MadinaNeural", "sardor": "uz-UZ-SardorNeural"},
}
MAX_SPOKEN_CHARS = 350
MAX_SPOKEN_LINES = 6
FIRST_SENTENCE_CHARS = 200
SYNTH_TIMEOUT = 25.0
EDGE_ATTEMPTS = 3  # NoAudioReceived у edge-tts бывает разовым
DETAILS = {"ru": "Подробности отправил в чат.", "uz": "Batafsil ma'lumotni chatga yubordim."}
UNAVAILABLE = {"ru": "Сейчас не могу ответить.", "uz": "Hozir javob bera olmayman."}

_URL = re.compile(r"https?://\S+|www\.\S+")
_TAG = re.compile(r"<[^>]+>")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN = re.compile(r"[*_`#>~]+")
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍←-⇿⬀-⯿]+")
_LIST_LINE = re.compile(r"^\s*(?:[•\-–—*]|\d+[.)])\s+")
_UZ_MARKERS = (
    "oʻ", "gʻ", "o'", "g'", "o‘", "g‘", " va ", " bu ", " bilan", " uchun", " bor", " yo'q", " yoq", "men ", "siz ",
    "rahmat", "salom", "xayr", "qanday", "nima", "kerak", "mumkin", "iltimos", "bugun", "ertaga", "soat", "vazifa",
    "xarajat", "kecha", "yaxshi", "bajarildi", "qo'shildi", "qoʻshildi", "eslatma",
)


class TtsUnavailable(Exception):
    """edge-tts или ffmpeg недоступны."""


@dataclass(frozen=True)
class Spoken:
    text: str  # что говорим в звонке
    details: bool  # полный текст надо отправить в Telegram
    lang: str


def detect_lang(text: str) -> str:
    """ru / uz / en по тексту ответа. Кириллица — русский; латиница с узбекскими признаками — узбекский."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "ru"
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    if cyr >= len(letters) / 2:
        return "ru"
    low = f" {text.lower()} "
    score = sum(1 for marker in _UZ_MARKERS if marker in low)
    return "uz" if score >= 1 else "en"


def pick_voice(lang: str, ru: str = "svetlana", uz: str = "madina") -> str:
    if lang == "uz":
        return VOICES["uz"].get(uz, VOICES["uz"]["madina"])
    return VOICES["ru"].get(ru, VOICES["ru"]["svetlana"])  # английский читает русский голос: отдельный не заказывали


def plain(text: str) -> str:
    text = unescape(_TAG.sub(" ", text or ""))
    return text


def structured(raw: str) -> bool:
    """Таблицы, логи, ссылки, длинные списки и код вслух не читаются."""
    lines = [ln for ln in plain(raw).splitlines() if ln.strip()]
    return bool(
        _URL.search(raw) or "```" in raw or "<pre>" in raw or "Traceback" in raw
        or sum(1 for ln in lines if "|" in ln) >= 2
        or sum(1 for ln in lines if _LIST_LINE.match(ln)) >= 4
    )


def _speech_clean(text: str) -> str:
    text = _FENCE.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _MARKDOWN.sub(" ", text)
    text = _EMOJI.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text)[:FIRST_SENTENCE_CHARS].strip()


def spoken(raw: str, lang: str | None = None) -> Spoken:
    """Короткая версия ответа для звонка. Если ответ длинный или это таблица, лог, ссылка — «подробности отправил в чат»."""
    body = plain(raw)
    lang = lang or detect_lang(body)
    key = "uz" if lang == "uz" else "ru"
    lines = [ln for ln in body.splitlines() if ln.strip()]
    long = len(body.strip()) > MAX_SPOKEN_CHARS or len(lines) > MAX_SPOKEN_LINES
    if not long and not structured(raw):
        return Spoken(_speech_clean(body) or DETAILS[key], False, lang)
    if structured(raw):
        return Spoken(DETAILS[key], True, lang)
    first = _first_sentence(_speech_clean(body))
    return Spoken(f"{first} {DETAILS[key]}".strip(), True, lang)


Mp3Source = Callable[[str, str], Awaitable[bytes]]


async def edge_mp3(text: str, voice: str) -> bytes:
    """Синтез в mp3 через edge-tts."""
    try:
        import edge_tts  # noqa: PLC0415
    except ImportError as exc:
        raise TtsUnavailable("edge-tts не установлен") from exc
    last = "пустой звук"
    for attempt in range(1, EDGE_ATTEMPTS + 1):
        data = bytearray()

        async def collect() -> None:
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk.get("type") == "audio":
                    data.extend(chunk["data"])

        try:
            await asyncio.wait_for(collect(), timeout=SYNTH_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — сеть, блокировка сервиса, таймаут, NoAudioReceived
            last = type(exc).__name__
        else:
            if data:
                return bytes(data)
            last = "пустой звук"
        log.warning("edge-tts: попытка %d из %d не удалась (%s)", attempt, EDGE_ATTEMPTS, last)
        if attempt < EDGE_ATTEMPTS:
            await asyncio.sleep(0.6 * attempt)
    raise TtsUnavailable(f"edge-tts: {last}")


async def _ffmpeg(args: list[str], data: bytes) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", "pipe:0", *args, "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(data), timeout=SYNTH_TIMEOUT)
    except FileNotFoundError as exc:
        raise TtsUnavailable("ffmpeg не найден в PATH (в Debian: apt install -y ffmpeg)") from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise TtsUnavailable(f"ffmpeg: {type(exc).__name__}") from exc
    if proc.returncode != 0 or not out:
        raise TtsUnavailable("ffmpeg: " + err.decode(errors="replace").strip()[:120])
    return out


async def to_pcm(audio: bytes) -> bytes:
    """Любой звук -> PCM 16 бит, 16 кГц, моно (формат на входе в звонок)."""
    return await _ffmpeg(["-ar", str(RATE), "-ac", "1", "-f", "s16le"], audio)


async def to_ogg_opus(audio: bytes) -> bytes:
    """Любой звук -> ogg opus для голосовых сообщений Telegram."""
    return await _ffmpeg(["-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-f", "ogg"], audio)


class Synthesizer:
    def __init__(self, voice_ru: str = "svetlana", voice_uz: str = "madina", source: Mp3Source = edge_mp3) -> None:
        self.voice_ru, self.voice_uz = voice_ru, voice_uz
        self._source = source
        self._fallback: dict[str, bytes] = {}  # фраза «не могу ответить» держим в памяти, пока edge-tts работал

    def voice_for(self, lang: str) -> str:
        return pick_voice(lang, self.voice_ru, self.voice_uz)

    async def mp3(self, text: str, lang: str) -> tuple[bytes, str]:
        voice = self.voice_for(lang)
        return await self._source(text, voice), voice

    async def pcm(self, text: str, lang: str) -> tuple[bytes, str]:
        audio, voice = await self.mp3(text, lang)
        return await to_pcm(audio), voice

    async def ogg(self, text: str, lang: str) -> bytes:
        audio, _ = await self.mp3(text, lang)
        return await to_ogg_opus(audio)

    async def warm_up(self) -> None:
        """Заранее озвучить «сейчас не могу ответить» на обоих языках, чтобы сказать её, когда сервис ляжет."""
        for lang in ("ru", "uz"):
            try:
                self._fallback[lang], _ = await self.pcm(UNAVAILABLE[lang], lang)
            except TtsUnavailable as exc:
                log.warning("Заготовку «не могу ответить» (%s) озвучить не удалось: %s", lang, exc)

    def unavailable_pcm(self, lang: str = "ru") -> bytes | None:
        return self._fallback.get(lang) or self._fallback.get("ru")
