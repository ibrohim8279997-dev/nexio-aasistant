"""Проверка SIP-службы без Linphone: звонит на неё так, как это делает клиент с digest-паролем.

    python scripts/sip_probe.py                  — на 127.0.0.1 и на SIP_BIND_ADDRESS
    python scripts/sip_probe.py 100.119.11.24    — на указанный адрес

Шаги: OPTIONS -> INVITE без пароля (ждём 401 с WWW-Authenticate) -> INVITE с паролем (ждём 200 OK) -> BYE.
Пароль берётся из .env и никуда не печатается. Если проходит всё, сервер в порядке, проблема в настройке Linphone."""
from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import ConfigError  # noqa: E402
from bot.voice.config import VoiceConfig  # noqa: E402


def md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()  # digest в SIP: MD5 по протоколу


def collect(sock: socket.socket, wait: float) -> list[str]:
    """Принять все ответы за wait секунд (100 Trying приходит раньше финального)."""
    end = time.time() + wait
    packets = []
    while time.time() < end:
        sock.settimeout(max(end - time.time(), 0.05))
        try:
            data, _ = sock.recvfrom(65535)
        except (socket.timeout, TimeoutError):
            break
        text = data.decode(errors="replace")
        packets.append(text)
        code = re.match(r"SIP/2.0 (\d+)", text)
        if code and int(code.group(1)) >= 200:
            break
    return packets


def status(packet: str) -> str:
    return packet.splitlines()[0] if packet else "—"


def header(packet: str, name: str) -> str | None:
    m = re.search(rf"(?im)^{name}:[ \t]*(.+)$", packet)
    return m.group(1).strip() if m else None


def sdp(ip: str) -> str:
    return ("v=0\r\no=probe 1 1 IN IP4 {ip}\r\ns=probe\r\nc=IN IP4 {ip}\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").format(ip=ip)


def request(method: str, user: str, host: str, port: int, lip: str, lport: int, callid: str, tag: str, cseq: int,
            to_tag: str | None = None, extra: str = "", body: str = "", branch: str | None = None) -> str:
    branch = branch or "z9hG4bK" + os.urandom(4).hex()
    to = f"<sip:{user}@{host}>" + (f";tag={to_tag}" if to_tag else "")
    head = (f"{method} sip:{user}@{host}:{port} SIP/2.0\r\nVia: SIP/2.0/UDP {lip}:{lport};branch={branch};rport\r\n"
            f"Max-Forwards: 70\r\nFrom: \"probe\" <sip:probe@{lip}>;tag={tag}\r\nTo: {to}\r\nCall-ID: {callid}\r\n"
            f"CSeq: {cseq} {method}\r\nContact: <sip:probe@{lip}:{lport}>\r\n{extra}")
    if body:
        head += f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}"
    else:
        head += "Content-Length: 0\r\n\r\n"
    return head


def probe(host: str, cfg: VoiceConfig) -> bool:
    print(f"\n=== {host}:{cfg.sip_port} ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, 0))
    except OSError as exc:
        print(f"✖ не удалось привязаться к {host}: {exc}")
        return False
    lport = sock.getsockname()[1]
    dest = (host, cfg.sip_port)
    ok = True

    sock.sendto(request("OPTIONS", cfg.sip_user, host, cfg.sip_port, host, lport, "p-opt-" + os.urandom(3).hex(), "o1", 1).encode(), dest)
    got = collect(sock, 3)
    print(("✔ " if got else "✖ ") + "OPTIONS: " + (status(got[-1]) if got else "нет ответа (служба не слушает этот адрес?)"))
    if not got:
        return False

    callid, tag = "probe-" + os.urandom(4).hex(), "t" + os.urandom(3).hex()
    sock.sendto(request("INVITE", cfg.sip_user, host, cfg.sip_port, host, lport, callid, tag, 1, body=sdp(host)).encode(), dest)
    got = collect(sock, 4)
    final = got[-1] if got else ""
    challenge = header(final, "WWW-Authenticate")
    print(f"{'✔' if status(final).startswith('SIP/2.0 401') else '✖'} INVITE без пароля: {status(final)}")
    if not challenge:
        print("✖ в ответе 401 НЕТ заголовка WWW-Authenticate: Linphone не поймёт, что нужен пароль. Пришлите этот вывод мне.")
        print("  Ответ целиком:\n" + "\n".join("  " + ln for ln in final.splitlines()[:20]))
        return False
    print(f"✔ WWW-Authenticate: {challenge}")
    to_tag = re.search(r"(?i)^To:.*;tag=([^;>\s]+)", final, re.M)
    sock.sendto(request("ACK", cfg.sip_user, host, cfg.sip_port, host, lport, callid, tag, 1,
                        to_tag.group(1) if to_tag else None).encode(), dest)

    nonce = re.search(r'nonce="([^"]+)"', challenge).group(1)
    realm = (re.search(r'realm="([^"]+)"', challenge) or re.search(r"realm=(\w+)", challenge)).group(1)
    uri = f"sip:{cfg.sip_user}@{host}:{cfg.sip_port}"
    nc, cnonce = "00000001", os.urandom(4).hex()
    ha1, ha2 = md5(f"{cfg.sip_user}:{realm}:{cfg.sip_password}"), md5(f"INVITE:{uri}")
    response = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
    auth = (f'Authorization: Digest username="{cfg.sip_user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'response="{response}", algorithm=MD5, cnonce="{cnonce}", qop=auth, nc={nc}\r\n')
    sock.sendto(request("INVITE", cfg.sip_user, host, cfg.sip_port, host, lport, callid, tag, 2, extra=auth, body=sdp(host)).encode(), dest)
    got = collect(sock, 6)
    final = got[-1] if got else ""
    accepted = status(final).startswith("SIP/2.0 200")
    ok = ok and accepted
    print(f"{'✔' if accepted else '✖'} INVITE с верным паролем: {status(final)}" + ("" if got else " (нет ответа)"))
    if accepted:
        to_tag = re.search(r"(?i)^To:.*;tag=([^;>\s]+)", final, re.M)
        tt = to_tag.group(1) if to_tag else None
        sock.sendto(request("ACK", cfg.sip_user, host, cfg.sip_port, host, lport, callid, tag, 2, tt).encode(), dest)
        time.sleep(0.5)
        sock.sendto(request("BYE", cfg.sip_user, host, cfg.sip_port, host, lport, callid, tag, 3, tt).encode(), dest)
        print("  (звонок закрыт командой BYE)")
    else:
        print("  Ответ целиком:\n" + "\n".join("  " + ln for ln in final.splitlines()[:20]))
    sock.close()
    return ok


def main() -> int:
    try:
        cfg = VoiceConfig.from_env()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return 2
    hosts = sys.argv[1:] or ["127.0.0.1", cfg.bind_address]
    results = [probe(h, cfg) for h in dict.fromkeys(hosts)]
    print("\nИТОГ:", "сервер принимает звонки с паролем, ищите причину в настройке Linphone" if all(results)
          else "есть проблемы на стороне сервера, пришлите вывод целиком")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
