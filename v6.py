import asyncio
import http.server
import json
import logging
import msvcrt
import ipaddress
import secrets
import ssl
import socketserver
import threading
import tempfile
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
import cv2
import pyaudio
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

WEBSOCKET_LOGGER = logging.getLogger("miclon.websocket")
WEBSOCKET_LOGGER.addHandler(logging.NullHandler())
WEBSOCKET_LOGGER.propagate = False

# --- CONFIGURATION ENCART TÉLÉPHONE ---
PHONE_AD_ENABLED = True
PHONE_AD_MODE = "rss"  # "gif", "partner", or "rss"
PHONE_AD_URL = "https://pixelfed.social/users/ShoesCat.atom"
PHONE_AD_ALT = "Contenu partenaire"
RSS_FEED_MAX_BYTES = 512 * 1024
RSS_FEED_MAX_ITEMS = 10
RSS_FEED_MAX_IMAGES_PER_ITEM = 4


def parse_rss_feed(payload):
    root = ET.fromstring(payload)

    def child_text(element, name):
        for child in element:
            if child.tag.rsplit("}", 1)[-1].casefold() == name:
                return (child.text or "").strip()
        return ""

    class FeedImageParser(HTMLParser):
        def __init__(self, add_image):
            super().__init__()
            self.add_image = add_image

        def handle_starttag(self, tag, attrs):
            if tag.casefold() not in ("img", "source"):
                return
            attributes = dict(attrs)
            self.add_image(attributes.get("src", ""))
            for candidate in (attributes.get("srcset") or "").split(","):
                self.add_image(candidate.strip().split(" ")[0])

    def item_images(element):
        images = []

        def add_image(candidate):
            candidate = (candidate or "").strip()
            if not candidate or len(images) >= RSS_FEED_MAX_IMAGES_PER_ITEM:
                return
            try:
                image_url = urllib.parse.urljoin(PHONE_AD_URL, candidate)
                parsed_url = urllib.parse.urlsplit(image_url)
            except ValueError:
                return
            if (
                parsed_url.scheme.casefold() != "https"
                or not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
            ):
                return
            if image_url not in images:
                images.append(image_url)

        html_parser = FeedImageParser(add_image)
        for descendant in element.iter():
            name = descendant.tag.rsplit("}", 1)[-1].casefold()
            media_type = descendant.attrib.get("type", "").casefold()
            media_kind = descendant.attrib.get("medium", "").casefold()
            if name in ("thumbnail", "img"):
                add_image(descendant.attrib.get("url", "") or descendant.attrib.get("src", ""))
            elif name == "enclosure" and media_type.startswith("image/"):
                add_image(descendant.attrib.get("url", ""))
            elif name in ("content", "link") and (
                media_type.startswith("image/")
                or media_kind == "image"
            ):
                add_image(
                    descendant.attrib.get("url", "")
                    or descendant.attrib.get("href", "")
                    or descendant.attrib.get("src", "")
                )

            if name in ("content", "description", "summary", "encoded"):
                html_parser.feed("".join(descendant.itertext()))
        html_parser.close()
        return images

    feed_title = child_text(root, "title")
    if not feed_title:
        channel = next(
            (
                element
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1].casefold() == "channel"
            ),
            None,
        )
        if channel is not None:
            feed_title = child_text(channel, "title")

    items = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() not in ("item", "entry"):
            continue

        title = child_text(element, "title") or "Sans titre"
        link = child_text(element, "link")
        if not link:
            fallback_link = ""
            for child in element:
                if child.tag.rsplit("}", 1)[-1].casefold() == "link":
                    candidate = child.attrib.get("href", "").strip()
                    relation = child.attrib.get("rel", "alternate").casefold()
                    if candidate and relation in ("", "alternate"):
                        link = candidate
                        break
                    if candidate and not fallback_link:
                        fallback_link = candidate
            link = link or fallback_link
        if link:
            link = urllib.parse.urljoin(PHONE_AD_URL, link)
            if urllib.parse.urlsplit(link).scheme.casefold() not in ("http", "https"):
                link = ""
        items.append(
            {"title": title, "link": link, "images": item_images(element)}
        )
        if len(items) >= RSS_FEED_MAX_ITEMS:
            break

    return {"title": feed_title or "Flux RSS", "items": items}


def load_rss_feed():
    feed_url = urllib.parse.urlsplit(PHONE_AD_URL)
    if (
        feed_url.scheme.casefold() != "https"
        or not feed_url.hostname
        or feed_url.username
        or feed_url.password
    ):
        raise ValueError("PHONE_AD_URL doit être une URL HTTPS absolue.")

    request = urllib.request.Request(
        PHONE_AD_URL,
        headers={"User-Agent": "Miclon RSS reader"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if urllib.parse.urlsplit(response.geturl()).scheme.casefold() != "https":
            raise ValueError("Le flux RSS a redirigé vers une URL non HTTPS.")
        payload = response.read(RSS_FEED_MAX_BYTES + 1)
    if len(payload) > RSS_FEED_MAX_BYTES:
        raise ValueError("Le flux RSS dépasse la taille maximale autorisée.")
    return parse_rss_feed(payload)

# --- CONFIGURATION AUDIO ---
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100
CHUNK = 1024
PAIRING_PIN = f"{secrets.randbelow(100_000_000):08d}"
SESSION_TOKEN = secrets.token_urlsafe(32)
SESSION_DURATION_SECONDS = 3 * 60 * 60
session_token_expires_at = 0.0
session_token_lock = threading.Lock()
PAIRING_ATTEMPT_LIMIT = 5
PAIRING_LOCKOUT_SECONDS = 60
pairing_attempts = {}
pairing_attempts_lock = threading.Lock()


def is_valid_session_token(token):
    if (
        not isinstance(token, str)
        or not token.isascii()
        or not secrets.compare_digest(token, SESSION_TOKEN)
    ):
        return False
    with session_token_lock:
        return time.monotonic() < session_token_expires_at


LANGUAGE_NAMES = {
    "fr": "Français",
    "en": "English",
    "es": "Español",
    "zh": "中文",
    "ko": "한국어",
    "ja": "日本語",
    "pt": "Português",
    "it": "Italiano",
    "de": "Deutsch",
    "de-at": "Deutsch (Österreich)",
    "nl": "Nederlands",
    "hu": "Magyar",
    "ro": "Română",
    "pl": "Polski",
    "ru": "Русский",
    "uk": "Українська",
    "lt": "Lietuvių",
    "el": "Ελληνικά",
}

PC_MESSAGES = {
    "fr": {
        "audio_output_selected": "Sortie audio sélectionnée : {name}",
        "audio_output_missing": "Sortie VB-CABLE introuvable : {outputs}",
        "language_changed": "Langue de l'application définie sur {language}.",
        "microphone_enabled": "Liaison activée depuis {address}",
        "microphone_suspended": "Liaison suspendue depuis {address}",
        "http_listening": "Écoute HTTPS sur 0.0.0.0:8080",
        "qr_instructions": "Scannez ce code avec l'appareil photo du téléphone :",
        "phone_authenticated": "Téléphone authentifié depuis {address}.",
        "audio_progress": "depuis {address} : messages={messages}, dernier={size} octets, total={total} octets",
        "websocket_closed": "Connexion fermée depuis {address} : code={code}, motif={reason}, messages={messages}, octets={total}",
        "websocket_error": "Erreur depuis {address} après {messages} messages / {total} octets : {error}",
        "server_ready": "=== SERVEUR PRÊT ===",
        "websocket_listening": "Écoute WebSocket sur 0.0.0.0:5005",
        "pairing_pin": "Code PIN : {pin}",
        "phone_url": "Sur votre téléphone, ouvrez : {url}",
        "certificate_warning": "Certificat auto-signé généré pour cette adresse. Le navigateur du téléphone affichera un avertissement : continuez uniquement si l'URL correspond à ce PC.",
        "start_microphone": "Cliquez sur « Démarrer le Micro »",
        "escape_to_quit": "Appuyez sur Échap pour quitter.",
        "stop_requested": "Arrêt demandé.",
        "server_stopping": "Arrêt du serveur.",
        "network_error": "Impossible de déterminer l'IP locale : {error}",
    },
    "en": {
        "audio_output_selected": "Audio output selected: {name}",
        "audio_output_missing": "VB-CABLE output not found: {outputs}",
        "language_changed": "Application language set to {language}.",
        "microphone_enabled": "Microphone link enabled from {address}",
        "microphone_suspended": "Microphone link suspended from {address}",
        "http_listening": "Listening on HTTPS at 0.0.0.0:8080",
        "qr_instructions": "Scan this code with your phone camera:",
        "phone_authenticated": "Phone authenticated from {address}.",
        "audio_progress": "from {address}: messages={messages}, last={size} bytes, total={total} bytes",
        "websocket_closed": "Connection closed from {address}: code={code}, reason={reason}, messages={messages}, bytes={total}",
        "websocket_error": "Error from {address} after {messages} messages / {total} bytes: {error}",
        "server_ready": "=== SERVER READY ===",
        "websocket_listening": "Listening on WebSocket at 0.0.0.0:5005",
        "pairing_pin": "Pairing PIN: {pin}",
        "phone_url": "On your phone, open: {url}",
        "certificate_warning": "A self-signed certificate was generated for this address. Your phone browser will show a warning; continue only if the URL matches this PC.",
        "start_microphone": "Select “Start Microphone”",
        "escape_to_quit": "Press Escape to quit.",
        "stop_requested": "Stop requested.",
        "server_stopping": "Stopping the server.",
        "network_error": "Could not determine the local IP address: {error}",
    },
    "es": {
        "audio_output_selected": "Salida de audio seleccionada: {name}",
        "audio_output_missing": "No se encontró la salida VB-CABLE: {outputs}",
        "language_changed": "Idioma de la aplicación: {language}.",
        "microphone_enabled": "Conexión del micrófono activada desde {address}",
        "microphone_suspended": "Conexión del micrófono suspendida desde {address}",
        "http_listening": "Escuchando por HTTPS en 0.0.0.0:8080",
        "qr_instructions": "Escanea este código con la cámara del teléfono:",
        "phone_authenticated": "Teléfono autenticado desde {address}.",
        "audio_progress": "desde {address}: mensajes={messages}, último={size} bytes, total={total} bytes",
        "websocket_closed": "Conexión cerrada desde {address}: código={code}, motivo={reason}, mensajes={messages}, bytes={total}",
        "websocket_error": "Error desde {address} tras {messages} mensajes / {total} bytes: {error}",
        "server_ready": "=== SERVIDOR LISTO ===",
        "websocket_listening": "Escuchando por WebSocket en 0.0.0.0:5005",
        "pairing_pin": "Código PIN: {pin}",
        "phone_url": "En el teléfono, abre: {url}",
        "certificate_warning": "Se generó un certificado autofirmado para esta dirección. El navegador del teléfono mostrará una advertencia: continúa solo si la URL corresponde a este PC.",
        "start_microphone": "Pulsa «Iniciar micrófono»",
        "escape_to_quit": "Pulsa Escape para salir.",
        "stop_requested": "Solicitud de cierre recibida.",
        "server_stopping": "Deteniendo el servidor.",
        "network_error": "No se pudo determinar la IP local: {error}",
    },
    "zh": {
        "audio_output_selected": "已选择音频输出：{name}",
        "audio_output_missing": "未找到 VB-CABLE 输出设备：{outputs}",
        "language_changed": "应用语言已设为{language}。",
        "microphone_enabled": "来自 {address} 的麦克风连接已启用",
        "microphone_suspended": "来自 {address} 的麦克风连接已暂停",
        "http_listening": "HTTPS 正在 0.0.0.0:8080 监听",
        "qr_instructions": "请用手机相机扫描此二维码：",
        "phone_authenticated": "手机已通过身份验证，来自 {address}。",
        "audio_progress": "来自 {address}：消息数={messages}，最新={size} 字节，总计={total} 字节",
        "websocket_closed": "来自 {address} 的连接已关闭：代码={code}，原因={reason}，消息数={messages}，字节数={total}",
        "websocket_error": "来自 {address} 的连接在 {messages} 条消息 / {total} 字节后出错：{error}",
        "server_ready": "=== 服务器已就绪 ===",
        "websocket_listening": "WebSocket 正在 0.0.0.0:5005 监听",
        "pairing_pin": "配对 PIN：{pin}",
        "phone_url": "请在手机上打开：{url}",
        "certificate_warning": "已为此地址生成自签名证书。手机浏览器会显示警告；只有在网址与此电脑匹配时才继续。",
        "start_microphone": "点击“启动麦克风”",
        "escape_to_quit": "按 Esc 键退出。",
        "stop_requested": "已请求停止。",
        "server_stopping": "正在停止服务器。",
        "network_error": "无法确定本地 IP 地址：{error}",
    },
    "ko": {
        "audio_output_selected": "오디오 출력 선택됨: {name}",
        "audio_output_missing": "VB-CABLE 출력을 찾을 수 없습니다: {outputs}",
        "language_changed": "앱 언어가 {language}(으)로 설정되었습니다.",
        "microphone_enabled": "{address}에서 마이크 연결이 활성화되었습니다",
        "microphone_suspended": "{address}에서 마이크 연결이 일시 중지되었습니다",
        "http_listening": "HTTPS 수신 중: 0.0.0.0:8080",
        "qr_instructions": "휴대전화 카메라로 이 코드를 스캔하세요:",
        "phone_authenticated": "{address}에서 휴대전화 인증이 완료되었습니다.",
        "audio_progress": "{address}에서: 메시지={messages}, 최근={size}바이트, 총={total}바이트",
        "websocket_closed": "{address} 연결 종료: 코드={code}, 이유={reason}, 메시지={messages}, 바이트={total}",
        "websocket_error": "{address}에서 {messages}개 메시지 / {total}바이트 이후 오류: {error}",
        "server_ready": "=== 서버 준비 완료 ===",
        "websocket_listening": "WebSocket 수신 중: 0.0.0.0:5005",
        "pairing_pin": "페어링 PIN: {pin}",
        "phone_url": "휴대전화에서 열기: {url}",
        "certificate_warning": "이 주소에 사용할 자체 서명 인증서가 생성되었습니다. URL이 이 PC와 일치하는 경우에만 계속하세요.",
        "start_microphone": "‘마이크 시작’을 선택하세요",
        "escape_to_quit": "종료하려면 Escape 키를 누르세요.",
        "stop_requested": "중지 요청을 받았습니다.",
        "server_stopping": "서버를 중지하는 중입니다.",
        "network_error": "로컬 IP 주소를 확인할 수 없습니다: {error}",
    },
    "ja": {
        "audio_output_selected": "選択した音声出力: {name}",
        "audio_output_missing": "VB-CABLE 出力が見つかりません: {outputs}",
        "language_changed": "アプリの言語を{language}に設定しました。",
        "microphone_enabled": "{address} からのマイク接続を有効にしました",
        "microphone_suspended": "{address} からのマイク接続を一時停止しました",
        "http_listening": "HTTPS を 0.0.0.0:8080 で待ち受けています",
        "qr_instructions": "スマートフォンのカメラでこのコードをスキャンしてください:",
        "phone_authenticated": "{address} からのスマートフォンを認証しました。",
        "audio_progress": "{address} から: メッセージ={messages}、最新={size} バイト、合計={total} バイト",
        "websocket_closed": "{address} から切断: コード={code}、理由={reason}、メッセージ={messages}、バイト={total}",
        "websocket_error": "{address} で {messages} 件 / {total} バイト受信後にエラー: {error}",
        "server_ready": "=== サーバーの準備ができました ===",
        "websocket_listening": "WebSocket を 0.0.0.0:5005 で待ち受けています",
        "pairing_pin": "ペアリング PIN: {pin}",
        "phone_url": "スマートフォンで開く: {url}",
        "certificate_warning": "このアドレス用に自己署名証明書を生成しました。URL がこの PC のものと一致する場合のみ続行してください。",
        "start_microphone": "「マイクを開始」を選択してください",
        "escape_to_quit": "終了するには Esc キーを押してください。",
        "stop_requested": "停止が要求されました。",
        "server_stopping": "サーバーを停止しています。",
        "network_error": "ローカル IP アドレスを特定できません: {error}",
    },
    "pt": {
        "audio_output_selected": "Saída de áudio selecionada: {name}",
        "audio_output_missing": "Saída VB-CABLE não encontrada: {outputs}",
        "language_changed": "Idioma da aplicação definido para {language}.",
        "microphone_enabled": "Ligação do microfone ativada por {address}",
        "microphone_suspended": "Ligação do microfone suspensa por {address}",
        "http_listening": "À escuta por HTTPS em 0.0.0.0:8080",
        "qr_instructions": "Leia este código com a câmara do telemóvel:",
        "phone_authenticated": "Telemóvel autenticado a partir de {address}.",
        "audio_progress": "de {address}: mensagens={messages}, última={size} bytes, total={total} bytes",
        "websocket_closed": "Ligação fechada por {address}: código={code}, motivo={reason}, mensagens={messages}, bytes={total}",
        "websocket_error": "Erro de {address} após {messages} mensagens / {total} bytes: {error}",
        "server_ready": "=== SERVIDOR PRONTO ===",
        "websocket_listening": "À escuta por WebSocket em 0.0.0.0:5005",
        "pairing_pin": "PIN de emparelhamento: {pin}",
        "phone_url": "No telemóvel, abra: {url}",
        "certificate_warning": "Foi gerado um certificado autoassinado para este endereço. Continue apenas se o URL corresponder a este PC.",
        "start_microphone": "Selecione «Iniciar microfone»",
        "escape_to_quit": "Prima Escape para sair.",
        "stop_requested": "Paragem solicitada.",
        "server_stopping": "A parar o servidor.",
        "network_error": "Não foi possível determinar o IP local: {error}",
    },
    "it": {
        "audio_output_selected": "Uscita audio selezionata: {name}",
        "audio_output_missing": "Uscita VB-CABLE non trovata: {outputs}",
        "language_changed": "Lingua dell'app impostata su {language}.",
        "microphone_enabled": "Collegamento microfono attivato da {address}",
        "microphone_suspended": "Collegamento microfono sospeso da {address}",
        "http_listening": "In ascolto HTTPS su 0.0.0.0:8080",
        "qr_instructions": "Scansiona questo codice con la fotocamera del telefono:",
        "phone_authenticated": "Telefono autenticato da {address}.",
        "audio_progress": "da {address}: messaggi={messages}, ultimo={size} byte, totale={total} byte",
        "websocket_closed": "Connessione chiusa da {address}: codice={code}, motivo={reason}, messaggi={messages}, byte={total}",
        "websocket_error": "Errore da {address} dopo {messages} messaggi / {total} byte: {error}",
        "server_ready": "=== SERVER PRONTO ===",
        "websocket_listening": "In ascolto WebSocket su 0.0.0.0:5005",
        "pairing_pin": "PIN di associazione: {pin}",
        "phone_url": "Apri sul telefono: {url}",
        "certificate_warning": "È stato generato un certificato autofirmato per questo indirizzo. Continua solo se l'URL corrisponde a questo PC.",
        "start_microphone": "Seleziona «Avvia microfono»",
        "escape_to_quit": "Premi Esc per uscire.",
        "stop_requested": "Arresto richiesto.",
        "server_stopping": "Arresto del server.",
        "network_error": "Impossibile determinare l'IP locale: {error}",
    },
    "de": {
        "audio_output_selected": "Audioausgabe ausgewählt: {name}",
        "audio_output_missing": "VB-CABLE-Ausgabe nicht gefunden: {outputs}",
        "language_changed": "Anwendungssprache auf {language} gesetzt.",
        "microphone_enabled": "Mikrofonverbindung aktiviert von {address}",
        "microphone_suspended": "Mikrofonverbindung pausiert von {address}",
        "http_listening": "HTTPS-Empfang auf 0.0.0.0:8080",
        "qr_instructions": "Scannen Sie diesen Code mit der Kamera Ihres Telefons:",
        "phone_authenticated": "Telefon authentifiziert von {address}.",
        "audio_progress": "von {address}: Nachrichten={messages}, zuletzt={size} Bytes, gesamt={total} Bytes",
        "websocket_closed": "Verbindung geschlossen von {address}: Code={code}, Grund={reason}, Nachrichten={messages}, Bytes={total}",
        "websocket_error": "Fehler von {address} nach {messages} Nachrichten / {total} Bytes: {error}",
        "server_ready": "=== SERVER BEREIT ===",
        "websocket_listening": "WebSocket-Empfang auf 0.0.0.0:5005",
        "pairing_pin": "Kopplungs-PIN: {pin}",
        "phone_url": "Öffnen Sie auf Ihrem Telefon: {url}",
        "certificate_warning": "Für diese Adresse wurde ein selbstsigniertes Zertifikat erstellt. Der Browser des Telefons zeigt eine Warnung an. Fahren Sie nur fort, wenn die URL zu diesem PC gehört.",
        "start_microphone": "Wählen Sie „Mikrofon starten“",
        "escape_to_quit": "Drücken Sie Escape zum Beenden.",
        "stop_requested": "Beenden angefordert.",
        "server_stopping": "Server wird beendet.",
        "network_error": "Lokale IP-Adresse konnte nicht ermittelt werden: {error}",
    },
    "nl": {
        "audio_output_selected": "Audio-uitvoer geselecteerd: {name}",
        "audio_output_missing": "VB-CABLE-uitvoer niet gevonden: {outputs}",
        "language_changed": "Applicatietaal ingesteld op {language}.",
        "microphone_enabled": "Microfoonverbinding ingeschakeld vanaf {address}",
        "microphone_suspended": "Microfoonverbinding onderbroken vanaf {address}",
        "http_listening": "Luistert via HTTPS op 0.0.0.0:8080",
        "qr_instructions": "Scan deze code met de camera van uw telefoon:",
        "phone_authenticated": "Telefoon geverifieerd vanaf {address}.",
        "audio_progress": "vanaf {address}: berichten={messages}, laatst={size} bytes, totaal={total} bytes",
        "websocket_closed": "Verbinding gesloten vanaf {address}: code={code}, reden={reason}, berichten={messages}, bytes={total}",
        "websocket_error": "Fout vanaf {address} na {messages} berichten / {total} bytes: {error}",
        "server_ready": "=== SERVER GEREED ===",
        "websocket_listening": "Luistert via WebSocket op 0.0.0.0:5005",
        "pairing_pin": "Koppelingspincode: {pin}",
        "phone_url": "Open op uw telefoon: {url}",
        "certificate_warning": "Voor dit adres is een zelfondertekend certificaat aangemaakt. De browser van uw telefoon toont een waarschuwing. Ga alleen verder als de URL bij deze pc hoort.",
        "start_microphone": "Selecteer ‘Microfoon starten’",
        "escape_to_quit": "Druk op Escape om af te sluiten.",
        "stop_requested": "Stop aangevraagd.",
        "server_stopping": "De server wordt gestopt.",
        "network_error": "Het lokale IP-adres kan niet worden bepaald: {error}",
    },
    "hu": {
        "audio_output_selected": "Kiválasztott hangkimenet: {name}",
        "audio_output_missing": "A VB-CABLE-kimenet nem található: {outputs}",
        "language_changed": "Az alkalmazás nyelve erre állítva: {language}.",
        "microphone_enabled": "Mikrofonkapcsolat engedélyezve innen: {address}",
        "microphone_suspended": "Mikrofonkapcsolat felfüggesztve innen: {address}",
        "http_listening": "HTTPS-figyelés a 0.0.0.0:8080 címen",
        "qr_instructions": "Olvassa be ezt a kódot telefonja kamerájával:",
        "phone_authenticated": "Telefon hitelesítve innen: {address}.",
        "audio_progress": "innen: {address}: üzenetek={messages}, legutóbbi={size} bájt, összesen={total} bájt",
        "websocket_closed": "Kapcsolat lezárva innen: {address}: kód={code}, ok={reason}, üzenetek={messages}, bájt={total}",
        "websocket_error": "Hiba innen: {address}, {messages} üzenet / {total} bájt után: {error}",
        "server_ready": "=== A SZERVER KÉSZ ===",
        "websocket_listening": "WebSocket-figyelés a 0.0.0.0:5005 címen",
        "pairing_pin": "Párosítási PIN-kód: {pin}",
        "phone_url": "Nyissa meg telefonján: {url}",
        "certificate_warning": "Ehhez a címhez önaláírt tanúsítvány készült. A telefon böngészője figyelmeztetést jelenít meg. Csak akkor folytassa, ha az URL ehhez a számítógéphez tartozik.",
        "start_microphone": "Válassza a „Mikrofon indítása” lehetőséget",
        "escape_to_quit": "A kilépéshez nyomja meg az Escape billentyűt.",
        "stop_requested": "Leállítás kérve.",
        "server_stopping": "A szerver leállítása.",
        "network_error": "A helyi IP-cím nem állapítható meg: {error}",
    },
    "ro": {
        "audio_output_selected": "Ieșire audio selectată: {name}",
        "audio_output_missing": "Ieșirea VB-CABLE nu a fost găsită: {outputs}",
        "language_changed": "Limba aplicației a fost setată la {language}.",
        "microphone_enabled": "Conexiune la microfon activată de la {address}",
        "microphone_suspended": "Conexiune la microfon suspendată de la {address}",
        "http_listening": "Se ascultă HTTPS pe 0.0.0.0:8080",
        "qr_instructions": "Scanați acest cod cu camera telefonului:",
        "phone_authenticated": "Telefon autentificat de la {address}.",
        "audio_progress": "de la {address}: mesaje={messages}, ultimul={size} octeți, total={total} octeți",
        "websocket_closed": "Conexiune închisă de la {address}: cod={code}, motiv={reason}, mesaje={messages}, octeți={total}",
        "websocket_error": "Eroare de la {address} după {messages} mesaje / {total} octeți: {error}",
        "server_ready": "=== SERVER PREGĂTIT ===",
        "websocket_listening": "Se ascultă WebSocket pe 0.0.0.0:5005",
        "pairing_pin": "PIN de asociere: {pin}",
        "phone_url": "Deschideți pe telefon: {url}",
        "certificate_warning": "A fost generat un certificat autosemnat pentru această adresă. Browserul telefonului va afișa un avertisment. Continuați numai dacă URL-ul corespunde acestui PC.",
        "start_microphone": "Selectați „Pornește microfonul”",
        "escape_to_quit": "Apăsați Escape pentru a ieși.",
        "stop_requested": "Oprire solicitată.",
        "server_stopping": "Se oprește serverul.",
        "network_error": "Nu s-a putut determina adresa IP locală: {error}",
    },
    "pl": {
        "audio_output_selected": "Wybrane wyjście audio: {name}",
        "audio_output_missing": "Nie znaleziono wyjścia VB-CABLE: {outputs}",
        "language_changed": "Język aplikacji ustawiono na {language}.",
        "microphone_enabled": "Połączenie mikrofonu włączone z {address}",
        "microphone_suspended": "Połączenie mikrofonu wstrzymane z {address}",
        "http_listening": "Nasłuchiwanie HTTPS na 0.0.0.0:8080",
        "qr_instructions": "Zeskanuj ten kod aparatem telefonu:",
        "phone_authenticated": "Telefon uwierzytelniony z {address}.",
        "audio_progress": "z {address}: wiadomości={messages}, ostatnia={size} bajtów, łącznie={total} bajtów",
        "websocket_closed": "Połączenie zamknięte z {address}: kod={code}, powód={reason}, wiadomości={messages}, bajty={total}",
        "websocket_error": "Błąd z {address} po {messages} wiadomościach / {total} bajtach: {error}",
        "server_ready": "=== SERWER GOTOWY ===",
        "websocket_listening": "Nasłuchiwanie WebSocket na 0.0.0.0:5005",
        "pairing_pin": "Kod PIN parowania: {pin}",
        "phone_url": "Otwórz na telefonie: {url}",
        "certificate_warning": "Dla tego adresu wygenerowano certyfikat z podpisem własnym. Przeglądarka telefonu wyświetli ostrzeżenie. Kontynuuj tylko wtedy, gdy adres URL należy do tego komputera.",
        "start_microphone": "Wybierz „Uruchom mikrofon”",
        "escape_to_quit": "Naciśnij Escape, aby zakończyć.",
        "stop_requested": "Zażądano zatrzymania.",
        "server_stopping": "Zatrzymywanie serwera.",
        "network_error": "Nie można ustalić lokalnego adresu IP: {error}",
    },
    "ru": {
        "audio_output_selected": "Выбран аудиовыход: {name}",
        "audio_output_missing": "Аудиовыход VB-CABLE не найден: {outputs}",
        "language_changed": "Язык приложения изменён на {language}.",
        "microphone_enabled": "Подключение микрофона включено с адреса {address}",
        "microphone_suspended": "Подключение микрофона приостановлено с адреса {address}",
        "http_listening": "Прослушивание HTTPS на 0.0.0.0:8080",
        "qr_instructions": "Отсканируйте этот код камерой телефона:",
        "phone_authenticated": "Телефон прошёл проверку с адреса {address}.",
        "audio_progress": "адрес {address}: сообщений={messages}, последнее={size} байт, всего={total} байт",
        "websocket_closed": "Соединение закрыто с адреса {address}: код={code}, причина={reason}, сообщений={messages}, байт={total}",
        "websocket_error": "Ошибка с адреса {address} после {messages} сообщений / {total} байт: {error}",
        "server_ready": "=== СЕРВЕР ГОТОВ ===",
        "websocket_listening": "Прослушивание WebSocket на 0.0.0.0:5005",
        "pairing_pin": "PIN-код сопряжения: {pin}",
        "phone_url": "Откройте на телефоне: {url}",
        "certificate_warning": "Для этого адреса создан самоподписанный сертификат. Браузер телефона покажет предупреждение. Продолжайте, только если URL соответствует этому компьютеру.",
        "start_microphone": "Нажмите «Запустить микрофон»",
        "escape_to_quit": "Нажмите Escape, чтобы выйти.",
        "stop_requested": "Запрошена остановка.",
        "server_stopping": "Остановка сервера.",
        "network_error": "Не удалось определить локальный IP-адрес: {error}",
    },
    "uk": {
        "audio_output_selected": "Вибрано аудіовихід: {name}",
        "audio_output_missing": "Аудіовихід VB-CABLE не знайдено: {outputs}",
        "language_changed": "Мову програми змінено на {language}.",
        "microphone_enabled": "Підключення мікрофона ввімкнено з адреси {address}",
        "microphone_suspended": "Підключення мікрофона призупинено з адреси {address}",
        "http_listening": "Очікування HTTPS-з’єднань на 0.0.0.0:8080",
        "qr_instructions": "Відскануйте цей код камерою телефона:",
        "phone_authenticated": "Телефон автентифіковано з адреси {address}.",
        "audio_progress": "з адреси {address}: повідомлень={messages}, останнє={size} байтів, усього={total} байтів",
        "websocket_closed": "З’єднання закрито з адреси {address}: код={code}, причина={reason}, повідомлень={messages}, байтів={total}",
        "websocket_error": "Помилка з адреси {address} після {messages} повідомлень / {total} байтів: {error}",
        "server_ready": "=== СЕРВЕР ГОТОВИЙ ===",
        "websocket_listening": "Очікування WebSocket-з’єднань на 0.0.0.0:5005",
        "pairing_pin": "PIN-код сполучення: {pin}",
        "phone_url": "Відкрийте на телефоні: {url}",
        "certificate_warning": "Для цієї адреси створено самопідписаний сертифікат. Браузер телефона покаже попередження. Продовжуйте, лише якщо URL відповідає цьому ПК.",
        "start_microphone": "Натисніть «Запустити мікрофон»",
        "escape_to_quit": "Натисніть Escape, щоб вийти.",
        "stop_requested": "Надійшов запит на зупинку.",
        "server_stopping": "Зупинка сервера.",
        "network_error": "Не вдалося визначити локальну IP-адресу: {error}",
    },
    "lt": {
        "audio_output_selected": "Pasirinkta garso išvestis: {name}",
        "audio_output_missing": "VB-CABLE išvestis nerasta: {outputs}",
        "language_changed": "Programos kalba pakeista į {language}.",
        "microphone_enabled": "Mikrofono ryšys įjungtas iš {address}",
        "microphone_suspended": "Mikrofono ryšys pristabdytas iš {address}",
        "http_listening": "Klausomasi HTTPS adresu 0.0.0.0:8080",
        "qr_instructions": "Nuskaitykite šį kodą telefono kamera:",
        "phone_authenticated": "Telefonas autentifikuotas iš {address}.",
        "audio_progress": "iš {address}: pranešimų={messages}, paskutinis={size} baitų, iš viso={total} baitų",
        "websocket_closed": "Ryšys uždarytas iš {address}: kodas={code}, priežastis={reason}, pranešimų={messages}, baitų={total}",
        "websocket_error": "Klaida iš {address} po {messages} pranešimų / {total} baitų: {error}",
        "server_ready": "=== SERVERIS PARUOŠTAS ===",
        "websocket_listening": "Klausomasi WebSocket adresu 0.0.0.0:5005",
        "pairing_pin": "Susiejimo PIN kodas: {pin}",
        "phone_url": "Telefone atidarykite: {url}",
        "certificate_warning": "Šiam adresui sukurtas savarankiškai pasirašytas sertifikatas. Telefono naršyklė parodys įspėjimą. Tęskite tik jei URL priklauso šiam kompiuteriui.",
        "start_microphone": "Pasirinkite „Paleisti mikrofoną“",
        "escape_to_quit": "Norėdami išeiti, paspauskite Escape.",
        "stop_requested": "Paprašyta sustabdyti.",
        "server_stopping": "Serveris stabdomas.",
        "network_error": "Nepavyko nustatyti vietinio IP adreso: {error}",
    },
    "el": {
        "audio_output_selected": "Επιλέχθηκε έξοδος ήχου: {name}",
        "audio_output_missing": "Δεν βρέθηκε έξοδος VB-CABLE: {outputs}",
        "language_changed": "Η γλώσσα της εφαρμογής ορίστηκε σε {language}.",
        "microphone_enabled": "Η σύνδεση μικροφώνου ενεργοποιήθηκε από {address}",
        "microphone_suspended": "Η σύνδεση μικροφώνου αναστάλθηκε από {address}",
        "http_listening": "Ακρόαση HTTPS στη διεύθυνση 0.0.0.0:8080",
        "qr_instructions": "Σαρώστε αυτόν τον κωδικό με την κάμερα του τηλεφώνου:",
        "phone_authenticated": "Έγινε έλεγχος ταυτότητας του τηλεφώνου από {address}.",
        "audio_progress": "από {address}: μηνύματα={messages}, τελευταίο={size} byte, σύνολο={total} byte",
        "websocket_closed": "Η σύνδεση έκλεισε από {address}: κωδικός={code}, αιτία={reason}, μηνύματα={messages}, byte={total}",
        "websocket_error": "Σφάλμα από {address} μετά από {messages} μηνύματα / {total} byte: {error}",
        "server_ready": "=== Ο ΔΙΑΚΟΜΙΣΤΗΣ ΕΙΝΑΙ ΕΤΟΙΜΟΣ ===",
        "websocket_listening": "Ακρόαση WebSocket στη διεύθυνση 0.0.0.0:5005",
        "pairing_pin": "PIN σύζευξης: {pin}",
        "phone_url": "Ανοίξτε στο τηλέφωνό σας: {url}",
        "certificate_warning": "Δημιουργήθηκε αυτοϋπογεγραμμένο πιστοποιητικό για αυτήν τη διεύθυνση. Το πρόγραμμα περιήγησης του τηλεφώνου θα εμφανίσει προειδοποίηση. Συνεχίστε μόνο αν η διεύθυνση URL αντιστοιχεί σε αυτόν τον υπολογιστή.",
        "start_microphone": "Επιλέξτε «Έναρξη μικροφώνου»",
        "escape_to_quit": "Πατήστε Escape για έξοδο.",
        "stop_requested": "Ζητήθηκε διακοπή.",
        "server_stopping": "Ο διακομιστής τερματίζεται.",
        "network_error": "Δεν ήταν δυνατός ο εντοπισμός της τοπικής διεύθυνσης IP: {error}",
    },
}
PC_MESSAGES["de-at"] = PC_MESSAGES["de"]

language_lock = threading.Lock()
current_language = "en"


def get_current_language():
    with language_lock:
        return current_language


def set_current_language(language):
    if language not in LANGUAGE_NAMES:
        raise ValueError(f"Unsupported language: {language}")
    global current_language
    with language_lock:
        current_language = language


def pc_text(key, **values):
    return PC_MESSAGES[get_current_language()][key].format(**values)


p = pyaudio.PyAudio()

def find_vb_cable_output():
    matches = []
    available_outputs = []

    for device_index in range(p.get_device_count()):
        device = p.get_device_info_by_index(device_index)
        if device["maxOutputChannels"] < CHANNELS:
            continue

        name = device["name"]
        available_outputs.append(name)
        normalized_name = name.casefold()
        if any(identifier in normalized_name for identifier in (
            "cable input (vb-audio",
            "vb-audio virtual cable",
            "vb-cable",
        )):
            matches.append((device_index, name))

    if not matches:
        outputs = ", ".join(available_outputs) or "aucune"
        raise RuntimeError(
            pc_text("audio_output_missing", outputs=outputs)
        )

    device_index, device_name = matches[0]
    print(pc_text("audio_output_selected", name=device_name))
    return device_index


VB_CABLE_OUTPUT_INDEX = find_vb_cable_output()
stream = p.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE,
    output=True,
    output_device_index=VB_CABLE_OUTPUT_INDEX,
    frames_per_buffer=CHUNK,
)
stream.start_stream()

speaker_monitor_enabled = False
speaker_monitor_stream = None
speaker_monitor_lock = threading.Lock()


def get_speaker_monitor_enabled():
    with speaker_monitor_lock:
        return speaker_monitor_enabled


def set_speaker_monitor_enabled(enabled):
    global speaker_monitor_enabled, speaker_monitor_stream
    with speaker_monitor_lock:
        if enabled == speaker_monitor_enabled:
            return

        if enabled:
            device = p.get_default_output_device_info()
            device_index = int(device["index"])
            if device_index == VB_CABLE_OUTPUT_INDEX:
                raise RuntimeError(
                    "La sortie audio par défaut est VB-CABLE, "
                    "pas les haut-parleurs du PC."
                )
            new_stream = p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                output=True,
                output_device_index=device_index,
                frames_per_buffer=CHUNK,
            )
            try:
                new_stream.start_stream()
            except Exception:
                new_stream.close()
                raise
            speaker_monitor_stream = new_stream
            speaker_monitor_enabled = True
            return

        if speaker_monitor_stream is not None:
            speaker_monitor_stream.stop_stream()
            speaker_monitor_stream.close()
            speaker_monitor_stream = None
        speaker_monitor_enabled = False


def write_audio(audio_data):
    stream.write(audio_data, exception_on_underflow=False)
    with speaker_monitor_lock:
        if speaker_monitor_enabled and speaker_monitor_stream is not None:
            speaker_monitor_stream.write(
                audio_data,
                exception_on_underflow=False,
            )


microphone_link_enabled = True
microphone_link_lock = threading.Lock()


def get_microphone_link_enabled():
    with microphone_link_lock:
        return microphone_link_enabled


def set_microphone_link_enabled(enabled):
    global microphone_link_enabled
    with microphone_link_lock:
        microphone_link_enabled = enabled


# --- PAGE WEB POUR LE TÉLÉPHONE ---
# Ce code HTML/JS est envoyé au téléphone pour capturer son micro
HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Microphone Sans Fil</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta id="themeColor" name="theme-color" content="#000000">
    <style>
        * { box-sizing: border-box; }
        body {
            display: flex;
            flex-direction: column;
            gap: 6px;
            width: 100%;
            height: 100vh;
            height: 100dvh;
            min-height: 0;
            margin: 0;
            padding: 6px;
            overflow: hidden;
            font-family: sans-serif;
            text-align: center;
            background: #000;
            color: #181818;
        }
        body > * { flex: 0 0 auto; min-width: 0; }
        button { max-width: 100%; padding: 10px 16px; font-size: 18px; cursor: pointer; border-radius: 10px; border: none; background: #007bff; color: white; }
        button:disabled { background: #ccc; }
        .meter { display: flex; justify-content: center; gap: 4px; max-width: 420px; margin: 12px auto; }
        .meter-bar { height: 24px; flex: 1; border-radius: 3px; background: #d0d0d0; }
        .meter-bar.active { background: #28a745; }
        .meter-bar.active.warning { background: #ffc107; }
        .meter-bar.active.danger { background: #dc3545; }
        #levelText { font-variant-numeric: tabular-nums; }
        .filter-control { max-width: 420px; margin: 8px auto; text-align: left; }
        .filter-control label { display: flex; justify-content: space-between; gap: 12px; }
        .filter-control input { width: 100%; margin: 12px 0; }
        .filter-help { color: #555; font-size: 14px; }
        .phone-ad-slot {
            display: flex;
            flex: 1 1 0;
            flex-direction: column;
            width: 100%;
            max-width: none;
            min-width: 0;
            min-height: 0;
            margin: 0 auto 52px;
            padding: 4px;
            overflow: hidden;
            border: 1px dashed #aaa;
            border-radius: 10px;
            background: #fff;
        }
        .phone-ad-slot[hidden] { display: none; }
        .phone-ad-label { margin: 0 0 8px; color: #555; font-size: 12px; text-transform: uppercase; }
        .phone-ad-content { flex: 1 1 0; min-width: 0; min-height: 0; overflow: hidden; }
        .phone-ad-content img { display: block; width: 100%; height: 100%; max-width: 100%; max-height: 100%; margin: 0 auto; object-fit: contain; }
        .phone-ad-content iframe { display: block; width: 100%; height: 100%; min-height: 0; border: 0; }
        .rss-feed { height: 100%; overflow-y: auto; color: #222; text-align: left; }
        .rss-feed h2 { margin: 0 0 6px; font-size: 16px; }
        .rss-feed ul { margin: 0; padding-left: 20px; }
        .rss-feed li { margin: 0 0 6px; }
        .rss-feed a { color: #0056b3; }
        .rss-feed img { width: 100%; height: auto; max-height: 180px; margin-top: 4px; object-fit: contain; }
        .language-picker { max-width: 420px; margin: 0 auto; }
        .language-picker-label { display: block; margin-bottom: 4px; }
        .language-buttons { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; }
        .language-buttons button { padding: 5px; min-width: 34px; font-size: 22px; line-height: 1; background: #e0e0e0; color: #222; }
        .language-buttons button[aria-pressed="true"] { outline: 3px solid #0056b3; outline-offset: 2px; }
        .language-buttons button:disabled { opacity: 0.6; }
        #languageStatus { min-height: 0; margin: 0; color: #a00; font-size: 14px; }
        .settings-panel {
            position: fixed;
            z-index: 5;
            inset: 56px 12px 64px;
            max-width: 560px;
            margin: auto;
            padding: 16px;
            overflow: auto;
            border: 1px solid #aaa;
            border-radius: 12px;
            background: #fff;
            color: #222;
            box-shadow: 0 4px 24px #0008;
            text-align: left;
        }
        .settings-panel[hidden] { display: none; }
        .settings-panel .language-picker,
        .settings-panel .filter-control { width: 100%; max-width: none; margin: 12px 0; }
        .dark-mode-control {
            display: flex;
            align-items: center;
            gap: 8px;
            margin: 16px 0;
        }
        .dark-mode-control input { width: 18px; height: 18px; }
        .speaker-monitor-control {
            display: flex;
            align-items: center;
            gap: 8px;
            margin: 16px 0;
        }
        .speaker-monitor-control input { width: 18px; height: 18px; }
        #speakerMonitorStatus { min-height: 1em; color: #a00; font-size: 14px; }
        body.dark-mode {
            background: #111;
            color: #f2f2f2;
        }
        body.dark-mode .settings-panel {
            background: #18181b;
            border-color: #3b3b3b;
            color: #f2f2f2;
            box-shadow: 0 4px 24px rgba(0, 0, 0, 0.5);
        }
        body.dark-mode .pair-pin-label,
        body.dark-mode .filter-help,
        body.dark-mode .phone-ad-label,
        body.dark-mode .phone-ad-slot .rss-feed,
        body.dark-mode #speakerMonitorStatus,
        body.dark-mode #languageStatus,
        body.dark-mode #linkStatus {
            color: #dfe7ff;
        }
        body.dark-mode .phone-ad-slot,
        body.dark-mode .rss-feed,
        body.dark-mode .language-buttons button,
        body.dark-mode .settings-panel .language-buttons button {
            background: #1e1e1e;
            color: #f2f2f2;
        }
        body.dark-mode .language-buttons button[aria-pressed="true"] {
            outline-color: #8ab4ff;
        }
        body.dark-mode .meter-bar {
            background: #404040;
        }
        body.dark-mode .meter-bar.active { background: #36d67b; }
        body.dark-mode .meter-bar.active.warning { background: #ffd75a; }
        body.dark-mode .meter-bar.active.danger { background: #ff6b6b; }
        body.dark-mode .phone-ad-slot {
            border-color: #555;
        }
        body.dark-mode .rss-feed a { color: #8ab4ff; }
        body.dark-mode .settings-panel input[type="range"] {
            accent-color: #8ab4ff;
        }
        #settingsBtn {
            position: static;
            z-index: 6;
            padding: 10px 12px;
            font-size: 18px;
            flex: 0 0 auto;
            background: #007bff;
            color: white;
        }
        h1, #status, #levelText { display: none; }
        #linkStatus { display: none; }
        #linkStatus[data-pair-error="true"] { display: block; color: #ff6b6b; }
        #pairControls { display: flex; flex: 1 1 0; flex-wrap: wrap; justify-content: center; align-items: center; gap: 6px; min-width: 0; }
        #pairControls[hidden] { display: none; }
        #pairPin { width: 120px; min-width: 0; }
        .pair-pin-label { color: #fff; }
        #levelMeter { display: none; }
        .filter-control input { margin: 4px 0; }
        .filter-help { margin: 4px 0 0; }
        .paired-actions button { height: 44px; }
        @media (max-height: 700px) {
            body { gap: 4px; }
            button { padding: 7px 12px; font-size: 16px; }
            .language-buttons { gap: 3px; }
            .language-buttons button { min-width: 30px; padding: 4px; font-size: 19px; }
            .filter-control { margin: 0 auto; }
            .filter-control label { font-size: 14px; }
            .filter-help { font-size: 12px; }
            .meter-bar { height: 16px; }
            .phone-ad-slot { padding: 4px; }
        }
        .paired-actions {
            position: fixed;
            z-index: 6;
            right: 6px;
            bottom: 6px;
            left: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        #linkBtn { display: none; background: #dc3545; }
        #linkBtn.suspended { background: #28a745; }
        body.paired button { background: #080808; color: #303030; }
        body.paired button:disabled { background: #050505; color: #151515; }
        body.paired #settingsBtn {
            background: #007bff;
            color: white;
        }
        body.paired .settings-panel button {
            background: #007bff;
            color: white;
        }
        body.paired .settings-panel .language-buttons button {
            background: #e0e0e0;
            color: #222;
        }
        body.paired #linkBtn {
            display: block;
            flex: 1 1 0;
            min-width: 0;
            padding: 0 10px;
            font-size: 14px;
            line-height: 1;
            overflow-wrap: anywhere;
            background: #dc3545;
            color: white;
        }
        body.paired #linkBtn.suspended { background: #28a745; }
    </style>
</head>
<body>
    <div class="paired-actions">
        <button id="settingsBtn" type="button" aria-label="Settings"
                aria-controls="settingsPanel" aria-expanded="false">⚙</button>
        <div id="pairControls">
            <label class="pair-pin-label" for="pairPin">PIN</label>
            <input id="pairPin" type="password" inputmode="numeric" maxlength="8"
                   autocomplete="one-time-code" aria-label="PIN">
        </div>
        <button id="linkBtn" type="button" aria-pressed="false" disabled>
            Suspend microphone link
        </button>
    </div>
    <section id="settingsPanel" class="settings-panel" aria-label="Settings" hidden>
        <button id="startBtn" type="button" data-i18n="start" hidden>Start Microphone</button>
        <button id="rePairBtn" type="button" data-i18n="repair" hidden>Re-pair device</button>
        <nav class="language-picker" aria-label="Language selection">
            <span id="languagePickerLabel" class="language-picker-label">Language:</span>
            <div id="languageButtons" class="language-buttons" role="group"
                 aria-labelledby="languagePickerLabel">
                <button type="button" data-language="fr" aria-label="Français" title="Français">🇫🇷</button>
                <button type="button" data-language="en" aria-label="English" title="English">🇬🇧</button>
                <button type="button" data-language="es" aria-label="Español" title="Español">🇪🇸</button>
                <button type="button" data-language="zh" aria-label="中文" title="中文">🇨🇳</button>
                <button type="button" data-language="ko" aria-label="한국어" title="한국어">🇰🇷</button>
                <button type="button" data-language="ja" aria-label="日本語" title="日本語">🇯🇵</button>
                <button type="button" data-language="pt" aria-label="Português" title="Português">🇵🇹</button>
                <button type="button" data-language="it" aria-label="Italiano" title="Italiano">🇮🇹</button>
                <button type="button" data-language="de" aria-label="Deutsch" title="Deutsch">🇩🇪</button>
                <button type="button" data-language="de-at" aria-label="Deutsch (Österreich)" title="Deutsch (Österreich)">🇦🇹</button>
                <button type="button" data-language="nl" aria-label="Nederlands" title="Nederlands">🇳🇱</button>
                <button type="button" data-language="hu" aria-label="Magyar" title="Magyar">🇭🇺</button>
                <button type="button" data-language="ro" aria-label="Română" title="Română">🇷🇴</button>
                <button type="button" data-language="pl" aria-label="Polski" title="Polski">🇵🇱</button>
                <button type="button" data-language="ru" aria-label="Русский" title="Русский">🇷🇺</button>
                <button type="button" data-language="uk" aria-label="Українська" title="Українська">🇺🇦</button>
                <button type="button" data-language="lt" aria-label="Lietuvių" title="Lietuvių">🇱🇹</button>
                <button type="button" data-language="el" aria-label="Ελληνικά" title="Ελληνικά">🇬🇷</button>
            </div>
            <p id="languageStatus" role="status" aria-live="polite"></p>
        </nav>
        <label class="dark-mode-control" for="darkModeToggle">
            <input id="darkModeToggle" type="checkbox">
            <span data-i18n="darkModeLabel">Dark mode</span>
        </label>
        <label class="speaker-monitor-control" for="speakerMonitor">
            <input id="speakerMonitor" type="checkbox" disabled>
            <span id="speakerMonitorLabel"></span>
        </label>
        <p id="speakerMonitorStatus" role="status" aria-live="polite"></p>
        <div class="filter-control">
            <label for="noiseFilter">
                <span data-i18n="filterLabel">Noise filter</span>
                <span id="noiseFilterValue">35 %</span>
            </label>
            <input id="noiseFilter" type="range" min="0" max="100" value="35"
                   aria-label="Noise filter strength" data-i18n-aria="filterAria">
            <p class="filter-help" data-i18n="filterHelp">
                Reduces background noise during silence. Too much filtering can also
                reduce quiet voices. Set to 0 to disable the filter.
            </p>
        </div>
        <div class="filter-control">
            <label for="pitchControl">
                <span data-i18n="pitchLabel">Pitch</span>
                <span id="pitchValue">0 st</span>
            </label>
            <input id="pitchControl" type="range" min="-12" max="12" step="1" value="0"
                   aria-label="Voice pitch" data-i18n-aria="pitchAria">
            <p class="filter-help" data-i18n="pitchHelp">
                Adjusts voice pitch in real time without changing audio duration.
            </p>
        </div>
    </section>
    <h1 data-i18n="heading">Wireless Microphone</h1>
    <p id="status">Disconnected</p>
    <p id="linkStatus" role="status">Waiting for link status...</p>
    <section id="phoneAdSlot" class="phone-ad-slot"
             aria-label="Espace publicitaire, partenaire ou flux RSS" hidden>
        <p class="phone-ad-label">Publicité / contenu partenaire / flux RSS</p>
        <div id="phoneAdContent" class="phone-ad-content"></div>
    </section>
    <p id="levelText">Microphone level: waiting</p>
    <div id="levelMeter" class="meter" role="meter" aria-label="Microphone sound level"
         aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"></div>
    <script>
        const translations = {
            en: {
                language: "Language:", languageAria: "Language selection",
                heading: "Wireless Microphone", pairLabel: "Pairing PIN shown in the PC console",
                pinAria: "Pairing PIN", pair: "Pair this device", start: "Start Microphone", repair: "Re-pair device",
                suspend: "Suspend microphone link", resume: "Resume microphone link",
                disconnected: "Disconnected", paired: "Device paired",
                pairError: "Pairing failed: {message}", linkWaiting: "Waiting for link status...",
                linkEnabled: "Microphone link enabled", linkSuspended: "Microphone link suspended",
                linkReadError: "Could not read link status: {message}",
                linkWriteError: "Could not change link status: {message}",
                levelWaiting: "Microphone level: waiting", meterLabel: "Microphone sound level",
                darkModeLabel: "Dark mode",
                filterLabel: "Noise filter", filterAria: "Noise filter strength",
                filterHelp: "Reduces background noise during silence. Too much filtering can also reduce quiet voices. Set to 0 to disable the filter.",
                pitchLabel: "Pitch", pitchAria: "Voice pitch",
                pitchHelp: "Adjusts voice pitch in real time without changing audio duration.",
                soundDetected: "sound detected", quietSignal: "silence or very weak signal",
                level: "Microphone level: {percent}%",
                languageError: "Could not sync language: {message}",
                insecure: "This page is open over HTTP from another device. Chrome on Android requires HTTPS to access the microphone. Configure a certificate trusted by the phone.",
                noMicrophoneApi: "This secure origin does not provide the microphone API. Check browser settings and site permissions.",
                permission: "Waiting for microphone permission...",
                connecting: "Microphone allowed, connecting to PC...",
                connected: "Connected! Audio is streaming...",
                audioError: "Audio error ({name}): {message}",
                websocketError: "Could not connect to the WebSocket.",
                disconnectedAgain: "Disconnected.",
                microphoneError: "Microphone unavailable ({name}): {message}"
            },
            fr: {
                language: "Langue :", languageAria: "Sélection de la langue",
                heading: "Microphone sans fil", pairLabel: "Code PIN affiché dans la console du PC",
                pinAria: "Code PIN", pair: "Associer cet appareil", start: "Démarrer le micro", repair: "Réassocier cet appareil",
                suspend: "Suspendre la liaison micro", resume: "Reprendre la liaison micro",
                disconnected: "Déconnecté", paired: "Appareil associé",
                pairError: "Association impossible : {message}", linkWaiting: "État de la liaison en attente...",
                linkEnabled: "Liaison micro activée", linkSuspended: "Liaison micro suspendue",
                linkReadError: "Impossible de lire l'état de la liaison : {message}",
                linkWriteError: "Impossible de modifier la liaison : {message}",
                levelWaiting: "Niveau du micro : en attente", meterLabel: "Niveau sonore du microphone",
                darkModeLabel: "Mode sombre",
                filterLabel: "Filtre antiparasite", filterAria: "Intensité du filtre antiparasite",
                filterHelp: "Réduit le bruit de fond pendant les silences. Une intensité trop forte peut aussi atténuer les voix faibles. Réglez à 0 pour désactiver le filtre.",
                pitchLabel: "Hauteur", pitchAria: "Hauteur de la voix",
                pitchHelp: "Ajuste la hauteur de la voix en temps réel sans changer la durée audio.",
                soundDetected: "son détecté", quietSignal: "silence ou signal très faible",
                level: "Niveau du micro : {percent}%",
                languageError: "Impossible de synchroniser la langue : {message}",
                insecure: "Cette page est ouverte en HTTP depuis un autre appareil. Chrome Android exige HTTPS pour accéder au microphone. Configurez un certificat approuvé par le téléphone.",
                noMicrophoneApi: "Cette origine sécurisée ne fournit pas l'API microphone. Vérifiez les paramètres du navigateur et les autorisations du site.",
                permission: "Autorisation du microphone...",
                connecting: "Micro autorisé, connexion au PC...",
                connected: "Connecté ! Transmission audio en cours...",
                audioError: "Erreur audio ({name}) : {message}",
                websocketError: "Connexion WebSocket impossible.",
                disconnectedAgain: "Déconnecté.",
                microphoneError: "Microphone indisponible ({name}) : {message}"
            },
            es: {
                language: "Idioma:", languageAria: "Selección de idioma",
                heading: "Micrófono inalámbrico", pairLabel: "Código PIN mostrado en la consola del PC",
                pinAria: "Código PIN", pair: "Asociar este dispositivo", start: "Iniciar micrófono", repair: "Volver a asociar este dispositivo",
                suspend: "Suspender conexión del micrófono", resume: "Reanudar conexión del micrófono",
                disconnected: "Desconectado", paired: "Dispositivo asociado",
                pairError: "No se pudo asociar: {message}", linkWaiting: "Esperando el estado de la conexión...",
                linkEnabled: "Conexión del micrófono activada", linkSuspended: "Conexión del micrófono suspendida",
                linkReadError: "No se pudo leer el estado: {message}",
                linkWriteError: "No se pudo cambiar el estado: {message}",
                levelWaiting: "Nivel del micrófono: esperando", meterLabel: "Nivel de sonido del micrófono",
                filterLabel: "Filtro de ruido", filterAria: "Intensidad del filtro de ruido",
                filterHelp: "Reduce el ruido de fondo durante los silencios. Una intensidad excesiva también puede atenuar las voces bajas. Ajusta a 0 para desactivar el filtro.",
                pitchLabel: "Tono", pitchAria: "Tono de voz",
                pitchHelp: "Ajusta el tono de la voz en tiempo real sin cambiar la duración del audio.",
                soundDetected: "sonido detectado", quietSignal: "silencio o señal muy débil",
                level: "Nivel del micrófono: {percent}%",
                languageError: "No se pudo sincronizar el idioma: {message}",
                insecure: "Esta página está abierta por HTTP desde otro dispositivo. Chrome en Android requiere HTTPS para acceder al micrófono. Configura un certificado de confianza para el teléfono.",
                noMicrophoneApi: "Este origen seguro no ofrece la API del micrófono. Comprueba la configuración del navegador y los permisos del sitio.",
                permission: "Esperando permiso para el micrófono...",
                connecting: "Micrófono autorizado, conectando al PC...",
                connected: "¡Conectado! Transmitiendo audio...",
                audioError: "Error de audio ({name}): {message}",
                websocketError: "No se pudo conectar al WebSocket.",
                disconnectedAgain: "Desconectado.",
                microphoneError: "Micrófono no disponible ({name}): {message}"
            },
            zh: {
                language: "语言：", languageAria: "语言选择",
                heading: "无线麦克风", pairLabel: "电脑控制台中显示的配对 PIN",
                pinAria: "配对 PIN", pair: "配对此设备", start: "启动麦克风", repair: "重新配对此设备",
                suspend: "暂停麦克风连接", resume: "恢复麦克风连接",
                disconnected: "未连接", paired: "设备已配对",
                pairError: "配对失败：{message}", linkWaiting: "正在等待连接状态……",
                linkEnabled: "麦克风连接已启用", linkSuspended: "麦克风连接已暂停",
                linkReadError: "无法读取连接状态：{message}",
                linkWriteError: "无法更改连接状态：{message}",
                levelWaiting: "麦克风音量：等待中", meterLabel: "麦克风音量",
                filterLabel: "降噪滤波器", filterAria: "降噪滤波强度",
                filterHelp: "在静音期间降低背景噪声。强度过高也可能减弱轻声。设为 0 可关闭滤波器。",
                pitchLabel: "音高", pitchAria: "音高调节",
                pitchHelp: "实时调整音高而不改变音频时长。",
                soundDetected: "检测到声音", quietSignal: "静音或信号很弱",
                level: "麦克风音量：{percent}%",
                languageError: "无法同步语言：{message}",
                insecure: "此页面通过 HTTP 从其他设备打开。Android 版 Chrome 要求使用 HTTPS 才能访问麦克风。请配置手机信任的证书。",
                noMicrophoneApi: "此安全来源未提供麦克风 API。请检查浏览器设置和网站权限。",
                permission: "正在等待麦克风权限……",
                connecting: "麦克风已授权，正在连接电脑……",
                connected: "已连接！正在传输音频……",
                audioError: "音频错误（{name}）：{message}",
                websocketError: "无法连接到 WebSocket。",
                disconnectedAgain: "已断开连接。",
                microphoneError: "麦克风不可用（{name}）：{message}"
            },
            ko: {
                language: "언어:", languageAria: "언어 선택",
                heading: "무선 마이크", pairLabel: "PC 콘솔에 표시된 페어링 PIN",
                pinAria: "페어링 PIN", pair: "이 기기 페어링", start: "마이크 시작", repair: "기기 다시 페어링",
                suspend: "마이크 연결 일시 중지", resume: "마이크 연결 다시 시작",
                disconnected: "연결 끊김", paired: "기기 페어링 완료",
                pairError: "페어링할 수 없습니다: {message}", linkWaiting: "연결 상태를 기다리는 중...",
                linkEnabled: "마이크 연결 활성화됨", linkSuspended: "마이크 연결 일시 중지됨",
                linkReadError: "연결 상태를 읽을 수 없습니다: {message}",
                linkWriteError: "연결 상태를 변경할 수 없습니다: {message}",
                levelWaiting: "마이크 음량: 대기 중", meterLabel: "마이크 음량",
                filterLabel: "소음 필터", filterAria: "소음 필터 강도",
                filterHelp: "무음 중 배경 소음을 줄입니다. 강도가 너무 높으면 작은 목소리도 줄어들 수 있습니다. 0으로 설정하면 필터가 꺼집니다.",
                pitchLabel: "음높이", pitchAria: "음높이 조절",
                pitchHelp: "오디오 길이를 바꾸지 않고 실시간으로 음높이를 조절합니다.",
                soundDetected: "소리 감지됨", quietSignal: "무음 또는 매우 약한 신호",
                level: "마이크 음량: {percent}%",
                languageError: "언어를 동기화할 수 없습니다: {message}",
                insecure: "이 페이지는 다른 기기에서 HTTP로 열렸습니다. Android Chrome에서 마이크를 사용하려면 HTTPS가 필요합니다. 휴대전화가 신뢰하는 인증서를 설정하세요.",
                noMicrophoneApi: "이 보안 출처에서 마이크 API를 제공하지 않습니다. 브라우저 설정과 사이트 권한을 확인하세요.",
                permission: "마이크 권한을 기다리는 중...",
                connecting: "마이크가 허용되었습니다. PC에 연결 중...",
                connected: "연결되었습니다! 오디오 전송 중...",
                audioError: "오디오 오류 ({name}): {message}",
                websocketError: "WebSocket에 연결할 수 없습니다.",
                disconnectedAgain: "연결이 끊겼습니다.",
                microphoneError: "마이크를 사용할 수 없습니다 ({name}): {message}"
            },
            ja: {
                language: "言語:", languageAria: "言語の選択",
                heading: "ワイヤレスマイク", pairLabel: "PC コンソールに表示された PIN",
                pinAria: "ペアリング PIN", pair: "この端末をペアリング", start: "マイクを開始", repair: "端末を再ペアリング",
                suspend: "マイク接続を一時停止", resume: "マイク接続を再開",
                disconnected: "未接続", paired: "端末をペアリングしました",
                pairError: "ペアリングできません: {message}", linkWaiting: "接続状態を確認しています...",
                linkEnabled: "マイク接続は有効です", linkSuspended: "マイク接続は一時停止中です",
                linkReadError: "接続状態を取得できません: {message}",
                linkWriteError: "接続状態を変更できません: {message}",
                levelWaiting: "マイクレベル: 待機中", meterLabel: "マイク音量",
                filterLabel: "ノイズフィルター", filterAria: "ノイズフィルターの強さ",
                filterHelp: "無音時の背景ノイズを低減します。強すぎると小さな声も弱まる場合があります。0 にすると無効になります。",
                pitchLabel: "ピッチ", pitchAria: "声のピッチ",
                pitchHelp: "音声の長さを変えずに、リアルタイムでピッチを調整します。",
                soundDetected: "音を検出", quietSignal: "無音または非常に弱い信号",
                level: "マイクレベル: {percent}%",
                languageError: "言語を同期できません: {message}",
                insecure: "このページは別の端末から HTTP で開かれています。Android 版 Chrome でマイクを使用するには HTTPS が必要です。スマートフォンで信頼されている証明書を設定してください。",
                noMicrophoneApi: "このセキュアな接続ではマイク API を利用できません。ブラウザーの設定とサイトの権限を確認してください。",
                permission: "マイクの許可を待っています...",
                connecting: "マイクが許可されました。PC に接続しています...",
                connected: "接続しました。音声を送信中...",
                audioError: "オーディオエラー ({name}): {message}",
                websocketError: "WebSocket に接続できません。",
                disconnectedAgain: "切断されました。",
                microphoneError: "マイクを利用できません ({name}): {message}"
            },
            pt: {
                language: "Idioma:", languageAria: "Seleção de idioma",
                heading: "Microfone sem fios", pairLabel: "PIN apresentado na consola do PC",
                pinAria: "PIN de emparelhamento", pair: "Associar este dispositivo", start: "Iniciar microfone", repair: "Voltar a associar este dispositivo",
                suspend: "Suspender ligação do microfone", resume: "Retomar ligação do microfone",
                disconnected: "Desligado", paired: "Dispositivo associado",
                pairError: "Não foi possível associar: {message}", linkWaiting: "A aguardar o estado da ligação...",
                linkEnabled: "Ligação do microfone ativa", linkSuspended: "Ligação do microfone suspensa",
                linkReadError: "Não foi possível ler o estado da ligação: {message}",
                linkWriteError: "Não foi possível alterar a ligação: {message}",
                levelWaiting: "Nível do microfone: a aguardar", meterLabel: "Nível de som do microfone",
                filterLabel: "Filtro de ruído", filterAria: "Intensidade do filtro de ruído",
                filterHelp: "Reduz o ruído de fundo durante o silêncio. Uma intensidade excessiva também pode reduzir vozes baixas. Defina como 0 para desativar o filtro.",
                pitchLabel: "Tom", pitchAria: "Tom da voz",
                pitchHelp: "Ajusta o tom da voz em tempo real sem alterar a duração do áudio.",
                soundDetected: "som detetado", quietSignal: "silêncio ou sinal muito fraco",
                level: "Nível do microfone: {percent}%",
                languageError: "Não foi possível sincronizar o idioma: {message}",
                insecure: "Esta página foi aberta por HTTP noutro dispositivo. O Chrome para Android exige HTTPS para aceder ao microfone. Configure um certificado de confiança no telemóvel.",
                noMicrophoneApi: "Esta origem segura não disponibiliza a API do microfone. Verifique as definições do navegador e as permissões do site.",
                permission: "A aguardar autorização do microfone...",
                connecting: "Microfone autorizado, a ligar ao PC...",
                connected: "Ligado! A transmitir áudio...",
                audioError: "Erro de áudio ({name}): {message}",
                websocketError: "Não foi possível ligar ao WebSocket.",
                disconnectedAgain: "Desligado.",
                microphoneError: "Microfone indisponível ({name}): {message}"
            },
            it: {
                language: "Lingua:", languageAria: "Selezione della lingua",
                heading: "Microfono wireless", pairLabel: "PIN mostrato nella console del PC",
                pinAria: "PIN di associazione", pair: "Associa questo dispositivo", start: "Avvia microfono", repair: "Associa di nuovo questo dispositivo",
                suspend: "Sospendi collegamento microfono", resume: "Riprendi collegamento microfono",
                disconnected: "Disconnesso", paired: "Dispositivo associato",
                pairError: "Impossibile associare: {message}", linkWaiting: "In attesa dello stato del collegamento...",
                linkEnabled: "Collegamento microfono attivo", linkSuspended: "Collegamento microfono sospeso",
                linkReadError: "Impossibile leggere lo stato del collegamento: {message}",
                linkWriteError: "Impossibile modificare il collegamento: {message}",
                levelWaiting: "Livello microfono: in attesa", meterLabel: "Livello audio del microfono",
                filterLabel: "Filtro antirumore", filterAria: "Intensità del filtro antirumore",
                filterHelp: "Riduce il rumore di fondo durante i silenzi. Un'intensità eccessiva può attenuare anche le voci deboli. Imposta 0 per disattivare il filtro.",
                pitchLabel: "Altezza", pitchAria: "Altezza della voce",
                pitchHelp: "Regola l'altezza della voce in tempo reale senza modificarne la durata.",
                soundDetected: "suono rilevato", quietSignal: "silenzio o segnale molto debole",
                level: "Livello microfono: {percent}%",
                languageError: "Impossibile sincronizzare la lingua: {message}",
                insecure: "Questa pagina è aperta tramite HTTP da un altro dispositivo. Chrome su Android richiede HTTPS per accedere al microfono. Configura un certificato considerato attendibile dal telefono.",
                noMicrophoneApi: "Questa origine sicura non fornisce l'API del microfono. Controlla le impostazioni del browser e le autorizzazioni del sito.",
                permission: "In attesa dell'autorizzazione del microfono...",
                connecting: "Microfono autorizzato, connessione al PC...",
                connected: "Connesso! Trasmissione audio in corso...",
                audioError: "Errore audio ({name}): {message}",
                websocketError: "Impossibile connettersi al WebSocket.",
                disconnectedAgain: "Disconnesso.",
                microphoneError: "Microfono non disponibile ({name}): {message}"
            },
            de: {
                language: "Sprache:", languageAria: "Sprachauswahl",
                heading: "Drahtloses Mikrofon", pairLabel: "PIN aus der PC-Konsole",
                pinAria: "Kopplungs-PIN", pair: "Dieses Gerät koppeln", start: "Mikrofon starten", repair: "Gerät erneut koppeln",
                suspend: "Mikrofonverbindung pausieren", resume: "Mikrofonverbindung fortsetzen",
                disconnected: "Nicht verbunden", paired: "Gerät gekoppelt",
                pairError: "Kopplung fehlgeschlagen: {message}", linkWaiting: "Warte auf Verbindungsstatus...",
                linkEnabled: "Mikrofonverbindung aktiviert", linkSuspended: "Mikrofonverbindung pausiert",
                linkReadError: "Verbindungsstatus konnte nicht gelesen werden: {message}",
                linkWriteError: "Verbindungsstatus konnte nicht geändert werden: {message}",
                levelWaiting: "Mikrofonpegel: wartet", meterLabel: "Mikrofonlautstärke",
                filterLabel: "Rauschfilter", filterAria: "Stärke des Rauschfilters",
                filterHelp: "Reduziert Hintergrundgeräusche während der Stille. Eine zu starke Filterung kann auch leise Stimmen dämpfen. Auf 0 setzen, um den Filter auszuschalten.",
                pitchLabel: "Tonhöhe", pitchAria: "Tonhöhe der Stimme",
                pitchHelp: "Ändert die Tonhöhe in Echtzeit, ohne die Audiodauer zu verändern.",
                soundDetected: "Ton erkannt", quietSignal: "Stille oder sehr schwaches Signal",
                level: "Mikrofonpegel: {percent}%",
                languageError: "Sprache konnte nicht synchronisiert werden: {message}",
                insecure: "Diese Seite wird auf einem anderen Gerät über HTTP geöffnet. Chrome für Android benötigt HTTPS für den Mikrofonzugriff. Richten Sie ein vom Telefon vertrauenswürdiges Zertifikat ein.",
                noMicrophoneApi: "Diese sichere Herkunft stellt keine Mikrofon-API bereit. Überprüfen Sie die Browsereinstellungen und Website-Berechtigungen.",
                permission: "Warte auf Mikrofonberechtigung...",
                connecting: "Mikrofon freigegeben, Verbindung zum PC wird hergestellt...",
                connected: "Verbunden! Audio wird übertragen...",
                audioError: "Audiofehler ({name}): {message}",
                websocketError: "Verbindung zum WebSocket nicht möglich.",
                disconnectedAgain: "Verbindung getrennt.",
                microphoneError: "Mikrofon nicht verfügbar ({name}): {message}"
            },
            nl: {
                language: "Taal:", languageAria: "Taalkeuze",
                heading: "Draadloze microfoon", pairLabel: "Koppelingspincode in de pc-console",
                pinAria: "Koppelingspincode", pair: "Dit apparaat koppelen", start: "Microfoon starten", repair: "Apparaat opnieuw koppelen",
                suspend: "Microfoonverbinding onderbreken", resume: "Microfoonverbinding hervatten",
                disconnected: "Niet verbonden", paired: "Apparaat gekoppeld",
                pairError: "Koppelen mislukt: {message}", linkWaiting: "Wachten op de verbindingsstatus...",
                linkEnabled: "Microfoonverbinding ingeschakeld", linkSuspended: "Microfoonverbinding onderbroken",
                linkReadError: "Verbindingsstatus kan niet worden gelezen: {message}",
                linkWriteError: "Verbindingsstatus kan niet worden gewijzigd: {message}",
                levelWaiting: "Microfoonniveau: wachten", meterLabel: "Microfoongeluidsniveau",
                filterLabel: "Ruisfilter", filterAria: "Sterkte van het ruisfilter",
                filterHelp: "Vermindert achtergrondruis tijdens stilte. Een te sterke filtering kan ook zachte stemmen dempen. Stel in op 0 om het filter uit te schakelen.",
                pitchLabel: "Toonhoogte", pitchAria: "Toonhoogte van de stem",
                pitchHelp: "Past de toonhoogte in realtime aan zonder de audioduur te wijzigen.",
                soundDetected: "geluid gedetecteerd", quietSignal: "stilte of zeer zwak signaal",
                level: "Microfoonniveau: {percent}%",
                languageError: "Taal kan niet worden gesynchroniseerd: {message}",
                insecure: "Deze pagina is via HTTP geopend vanaf een ander apparaat. Chrome op Android vereist HTTPS voor toegang tot de microfoon. Stel een certificaat in dat door de telefoon wordt vertrouwd.",
                noMicrophoneApi: "Deze beveiligde oorsprong biedt geen microfoon-API. Controleer de browserinstellingen en sitemachtigingen.",
                permission: "Wachten op toestemming voor de microfoon...",
                connecting: "Microfoon toegestaan, verbinden met de pc...",
                connected: "Verbonden! Audio wordt verzonden...",
                audioError: "Audiofout ({name}): {message}",
                websocketError: "Kan geen verbinding maken met de WebSocket.",
                disconnectedAgain: "Verbinding verbroken.",
                microphoneError: "Microfoon niet beschikbaar ({name}): {message}"
            },
            hu: {
                language: "Nyelv:", languageAria: "Nyelvválasztás",
                heading: "Vezeték nélküli mikrofon", pairLabel: "A párosítási PIN a PC konzolján látható",
                pinAria: "Párosítási PIN", pair: "Eszköz párosítása", start: "Mikrofon indítása", repair: "Eszköz újbóli párosítása",
                suspend: "Mikrofonkapcsolat felfüggesztése", resume: "Mikrofonkapcsolat folytatása",
                disconnected: "Nincs kapcsolat", paired: "Eszköz párosítva",
                pairError: "Sikertelen párosítás: {message}", linkWaiting: "Várakozás a kapcsolat állapotára...",
                linkEnabled: "Mikrofonkapcsolat engedélyezve", linkSuspended: "Mikrofonkapcsolat felfüggesztve",
                linkReadError: "A kapcsolat állapota nem olvasható: {message}",
                linkWriteError: "A kapcsolat állapota nem módosítható: {message}",
                levelWaiting: "Mikrofonszint: várakozás", meterLabel: "Mikrofon hangerőszintje",
                filterLabel: "Zajszűrő", filterAria: "Zajszűrő erőssége",
                filterHelp: "Csendben csökkenti a háttérzajt. A túl erős szűrés a halk hangokat is elnyomhatja. A szűrő kikapcsolásához állítsa 0-ra.",
                pitchLabel: "Hangmagasság", pitchAria: "Hangmagasság",
                pitchHelp: "Valós időben módosítja a hangmagasságot a hang időtartamának megváltoztatása nélkül.",
                soundDetected: "hang észlelve", quietSignal: "csend vagy nagyon gyenge jel",
                level: "Mikrofonszint: {percent}%",
                languageError: "A nyelv szinkronizálása nem sikerült: {message}",
                insecure: "Ez az oldal HTTP-n keresztül nyílt meg egy másik eszközről. Androidon a Chrome mikrofonhasználathoz HTTPS-t igényel. Állítson be a telefon által megbízhatónak ítélt tanúsítványt.",
                noMicrophoneApi: "Ez a biztonságos eredet nem biztosít mikrofon-API-t. Ellenőrizze a böngésző beállításait és a webhely engedélyeit.",
                permission: "Várakozás a mikrofon engedélyére...",
                connecting: "Mikrofon engedélyezve, csatlakozás a számítógéphez...",
                connected: "Csatlakoztatva! Hangátvitel folyamatban...",
                audioError: "Hanghiba ({name}): {message}",
                websocketError: "Nem sikerült csatlakozni a WebSockethez.",
                disconnectedAgain: "Kapcsolat megszakadt.",
                microphoneError: "A mikrofon nem érhető el ({name}): {message}"
            },
            ro: {
                language: "Limbă:", languageAria: "Selectarea limbii",
                heading: "Microfon wireless", pairLabel: "PIN-ul de asociere afișat în consola PC-ului",
                pinAria: "PIN de asociere", pair: "Asociază acest dispozitiv", start: "Pornește microfonul", repair: "Asociază din nou acest dispozitiv",
                suspend: "Suspendă conexiunea microfonului", resume: "Reia conexiunea microfonului",
                disconnected: "Deconectat", paired: "Dispozitiv asociat",
                pairError: "Asocierea a eșuat: {message}", linkWaiting: "Se așteaptă starea conexiunii...",
                linkEnabled: "Conexiunea microfonului este activată", linkSuspended: "Conexiunea microfonului este suspendată",
                linkReadError: "Starea conexiunii nu poate fi citită: {message}",
                linkWriteError: "Starea conexiunii nu poate fi modificată: {message}",
                levelWaiting: "Nivelul microfonului: în așteptare", meterLabel: "Nivelul sonor al microfonului",
                filterLabel: "Filtru de zgomot", filterAria: "Intensitatea filtrului de zgomot",
                filterHelp: "Reduce zgomotul de fundal în timpul liniștii. Filtrarea prea puternică poate reduce și vocile încete. Setați la 0 pentru a dezactiva filtrul.",
                pitchLabel: "Tonalitate", pitchAria: "Tonalitatea vocii",
                pitchHelp: "Ajustează tonalitatea în timp real fără a schimba durata audio.",
                soundDetected: "sunet detectat", quietSignal: "liniște sau semnal foarte slab",
                level: "Nivelul microfonului: {percent}%",
                languageError: "Limba nu a putut fi sincronizată: {message}",
                insecure: "Această pagină este deschisă prin HTTP de pe alt dispozitiv. Chrome pentru Android necesită HTTPS pentru accesul la microfon. Configurați un certificat de încredere pentru telefon.",
                noMicrophoneApi: "Această origine securizată nu oferă API-ul pentru microfon. Verificați setările browserului și permisiunile site-ului.",
                permission: "Se așteaptă permisiunea pentru microfon...",
                connecting: "Microfon autorizat, conectare la PC...",
                connected: "Conectat! Se transmite sunetul...",
                audioError: "Eroare audio ({name}): {message}",
                websocketError: "Conectarea la WebSocket a eșuat.",
                disconnectedAgain: "Deconectat.",
                microphoneError: "Microfon indisponibil ({name}): {message}"
            },
            pl: {
                language: "Język:", languageAria: "Wybór języka",
                heading: "Mikrofon bezprzewodowy", pairLabel: "Kod PIN parowania widoczny w konsoli komputera",
                pinAria: "Kod PIN parowania", pair: "Sparuj to urządzenie", start: "Uruchom mikrofon", repair: "Sparuj ponownie to urządzenie",
                suspend: "Wstrzymaj połączenie mikrofonu", resume: "Wznów połączenie mikrofonu",
                disconnected: "Rozłączono", paired: "Urządzenie sparowane",
                pairError: "Nie udało się sparować: {message}", linkWaiting: "Oczekiwanie na stan połączenia...",
                linkEnabled: "Połączenie mikrofonu włączone", linkSuspended: "Połączenie mikrofonu wstrzymane",
                linkReadError: "Nie można odczytać stanu połączenia: {message}",
                linkWriteError: "Nie można zmienić stanu połączenia: {message}",
                levelWaiting: "Poziom mikrofonu: oczekiwanie", meterLabel: "Poziom dźwięku mikrofonu",
                filterLabel: "Filtr szumów", filterAria: "Siła filtra szumów",
                filterHelp: "Zmniejsza hałas w tle podczas ciszy. Zbyt silne filtrowanie może również tłumić ciche głosy. Ustaw 0, aby wyłączyć filtr.",
                pitchLabel: "Wysokość", pitchAria: "Wysokość głosu",
                pitchHelp: "Zmienia wysokość głosu w czasie rzeczywistym bez zmiany czasu trwania dźwięku.",
                soundDetected: "wykryto dźwięk", quietSignal: "cisza lub bardzo słaby sygnał",
                level: "Poziom mikrofonu: {percent}%",
                languageError: "Nie można zsynchronizować języka: {message}",
                insecure: "Ta strona została otwarta przez HTTP z innego urządzenia. Chrome na Androidzie wymaga HTTPS, aby uzyskać dostęp do mikrofonu. Skonfiguruj certyfikat zaufany przez telefon.",
                noMicrophoneApi: "To bezpieczne źródło nie udostępnia interfejsu mikrofonu. Sprawdź ustawienia przeglądarki i uprawnienia witryny.",
                permission: "Oczekiwanie na uprawnienia mikrofonu...",
                connecting: "Mikrofon dozwolony, łączenie z komputerem...",
                connected: "Połączono! Trwa przesyłanie dźwięku...",
                audioError: "Błąd dźwięku ({name}): {message}",
                websocketError: "Nie można połączyć się z WebSocketem.",
                disconnectedAgain: "Rozłączono.",
                microphoneError: "Mikrofon niedostępny ({name}): {message}"
            },
            ru: {
                language: "Язык:", languageAria: "Выбор языка",
                heading: "Беспроводной микрофон", pairLabel: "PIN-код сопряжения показан в консоли ПК",
                pinAria: "PIN-код сопряжения", pair: "Подключить это устройство", start: "Запустить микрофон", repair: "Повторно подключить устройство",
                suspend: "Приостановить подключение микрофона", resume: "Возобновить подключение микрофона",
                disconnected: "Нет подключения", paired: "Устройство подключено",
                pairError: "Не удалось выполнить сопряжение: {message}", linkWaiting: "Ожидание состояния подключения...",
                linkEnabled: "Подключение микрофона включено", linkSuspended: "Подключение микрофона приостановлено",
                linkReadError: "Не удалось получить состояние подключения: {message}",
                linkWriteError: "Не удалось изменить состояние подключения: {message}",
                levelWaiting: "Уровень микрофона: ожидание", meterLabel: "Уровень звука микрофона",
                filterLabel: "Фильтр шума", filterAria: "Сила фильтра шума",
                filterHelp: "Уменьшает фоновый шум во время тишины. Слишком сильная фильтрация может также приглушать тихие голоса. Установите 0, чтобы отключить фильтр.",
                pitchLabel: "Высота тона", pitchAria: "Высота голоса",
                pitchHelp: "Меняет высоту голоса в реальном времени без изменения длительности аудио.",
                soundDetected: "обнаружен звук", quietSignal: "тишина или очень слабый сигнал",
                level: "Уровень микрофона: {percent}%",
                languageError: "Не удалось синхронизировать язык: {message}",
                insecure: "Эта страница открыта по HTTP с другого устройства. Для доступа к микрофону Chrome на Android требует HTTPS. Настройте сертификат, которому доверяет телефон.",
                noMicrophoneApi: "Этот защищённый источник не предоставляет API микрофона. Проверьте настройки браузера и разрешения сайта.",
                permission: "Ожидание разрешения на использование микрофона...",
                connecting: "Доступ к микрофону разрешён, подключение к ПК...",
                connected: "Подключено! Передача звука...",
                audioError: "Ошибка аудио ({name}): {message}",
                websocketError: "Не удалось подключиться к WebSocket.",
                disconnectedAgain: "Соединение разорвано.",
                microphoneError: "Микрофон недоступен ({name}): {message}"
            },
            uk: {
                language: "Мова:", languageAria: "Вибір мови",
                heading: "Бездротовий мікрофон", pairLabel: "PIN-код сполучення показано в консолі ПК",
                pinAria: "PIN-код сполучення", pair: "Сполучити цей пристрій", start: "Запустити мікрофон", repair: "Повторно сполучити пристрій",
                suspend: "Призупинити підключення мікрофона", resume: "Відновити підключення мікрофона",
                disconnected: "Не підключено", paired: "Пристрій сполучено",
                pairError: "Не вдалося сполучити: {message}", linkWaiting: "Очікування стану підключення...",
                linkEnabled: "Підключення мікрофона ввімкнено", linkSuspended: "Підключення мікрофона призупинено",
                linkReadError: "Не вдалося прочитати стан підключення: {message}",
                linkWriteError: "Не вдалося змінити стан підключення: {message}",
                levelWaiting: "Рівень мікрофона: очікування", meterLabel: "Рівень звуку мікрофона",
                filterLabel: "Фільтр шуму", filterAria: "Інтенсивність фільтра шуму",
                filterHelp: "Зменшує фоновий шум під час тиші. Надмірна фільтрація може також приглушувати тихі голоси. Установіть 0, щоб вимкнути фільтр.",
                pitchLabel: "Висота тону", pitchAria: "Висота голосу",
                pitchHelp: "Змінює висоту голосу в реальному часі без зміни тривалості аудіо.",
                soundDetected: "звук виявлено", quietSignal: "тиша або дуже слабкий сигнал",
                level: "Рівень мікрофона: {percent}%",
                languageError: "Не вдалося синхронізувати мову: {message}",
                insecure: "Цю сторінку відкрито через HTTP з іншого пристрою. Для доступу до мікрофона Chrome на Android вимагає HTTPS. Налаштуйте сертифікат, якому довіряє телефон.",
                noMicrophoneApi: "Це захищене джерело не надає API мікрофона. Перевірте налаштування браузера та дозволи сайту.",
                permission: "Очікування дозволу на використання мікрофона...",
                connecting: "Доступ до мікрофона дозволено, підключення до ПК...",
                connected: "Підключено! Триває передавання звуку...",
                audioError: "Помилка аудіо ({name}): {message}",
                websocketError: "Не вдалося підключитися до WebSocket.",
                disconnectedAgain: "З’єднання розірвано.",
                microphoneError: "Мікрофон недоступний ({name}): {message}"
            },
            lt: {
                language: "Kalba:", languageAria: "Kalbos pasirinkimas",
                heading: "Belaidis mikrofonas", pairLabel: "Susiejimo PIN rodomas kompiuterio konsolėje",
                pinAria: "Susiejimo PIN", pair: "Susieti šį įrenginį", start: "Paleisti mikrofoną", repair: "Susieti įrenginį iš naujo",
                suspend: "Pristabdyti mikrofono ryšį", resume: "Atnaujinti mikrofono ryšį",
                disconnected: "Atjungta", paired: "Įrenginys susietas",
                pairError: "Nepavyko susieti: {message}", linkWaiting: "Laukiama ryšio būsenos...",
                linkEnabled: "Mikrofono ryšys įjungtas", linkSuspended: "Mikrofono ryšys pristabdytas",
                linkReadError: "Nepavyko nuskaityti ryšio būsenos: {message}",
                linkWriteError: "Nepavyko pakeisti ryšio būsenos: {message}",
                levelWaiting: "Mikrofono lygis: laukiama", meterLabel: "Mikrofono garso lygis",
                filterLabel: "Triukšmo filtras", filterAria: "Triukšmo filtro stiprumas",
                filterHelp: "Tylos metu sumažina foninį triukšmą. Per stiprus filtravimas gali slopinti ir tylius balsus. Norėdami išjungti filtrą, nustatykite 0.",
                pitchLabel: "Garso aukštis", pitchAria: "Balso aukštis",
                pitchHelp: "Reguliuoja balso aukštį realiuoju laiku nekeisdamas garso trukmės.",
                soundDetected: "aptiktas garsas", quietSignal: "tyla arba labai silpnas signalas",
                level: "Mikrofono lygis: {percent}%",
                languageError: "Nepavyko sinchronizuoti kalbos: {message}",
                insecure: "Šis puslapis kitame įrenginyje atidarytas per HTTP. „Chrome“ sistemoje „Android“ mikrofonui pasiekti reikia HTTPS. Nustatykite telefonu patikimą sertifikatą.",
                noMicrophoneApi: "Ši saugi kilmė neteikia mikrofono API. Patikrinkite naršyklės nustatymus ir svetainės leidimus.",
                permission: "Laukiama mikrofono leidimo...",
                connecting: "Mikrofonas leistas, jungiamasi prie kompiuterio...",
                connected: "Prisijungta! Siunčiamas garsas...",
                audioError: "Garso klaida ({name}): {message}",
                websocketError: "Nepavyko prisijungti prie „WebSocket“.",
                disconnectedAgain: "Ryšys atjungtas.",
                microphoneError: "Mikrofonas nepasiekiamas ({name}): {message}"
            },
            el: {
                language: "Γλώσσα:", languageAria: "Επιλογή γλώσσας",
                heading: "Ασύρματο μικρόφωνο", pairLabel: "Το PIN σύζευξης εμφανίζεται στην κονσόλα του υπολογιστή",
                pinAria: "PIN σύζευξης", pair: "Σύζευξη αυτής της συσκευής", start: "Έναρξη μικροφώνου", repair: "Επανασύζευξη συσκευής",
                suspend: "Αναστολή σύνδεσης μικροφώνου", resume: "Συνέχιση σύνδεσης μικροφώνου",
                disconnected: "Αποσυνδεδεμένο", paired: "Η συσκευή συζεύχθηκε",
                pairError: "Αποτυχία σύζευξης: {message}", linkWaiting: "Αναμονή για την κατάσταση σύνδεσης...",
                linkEnabled: "Η σύνδεση μικροφώνου ενεργοποιήθηκε", linkSuspended: "Η σύνδεση μικροφώνου ανεστάλη",
                linkReadError: "Δεν ήταν δυνατή η ανάγνωση της κατάστασης σύνδεσης: {message}",
                linkWriteError: "Δεν ήταν δυνατή η αλλαγή της κατάστασης σύνδεσης: {message}",
                levelWaiting: "Στάθμη μικροφώνου: αναμονή", meterLabel: "Στάθμη ήχου μικροφώνου",
                filterLabel: "Φίλτρο θορύβου", filterAria: "Ένταση φίλτρου θορύβου",
                filterHelp: "Μειώνει τον θόρυβο παρασκηνίου κατά τη σιωπή. Η υπερβολική ένταση μπορεί επίσης να μειώσει τις χαμηλές φωνές. Ορίστε την τιμή σε 0 για απενεργοποίηση του φίλτρου.",
                pitchLabel: "Τόνος", pitchAria: "Τόνος φωνής",
                pitchHelp: "Ρυθμίζει τον τόνο σε πραγματικό χρόνο χωρίς να αλλάζει τη διάρκεια του ήχου.",
                soundDetected: "εντοπίστηκε ήχος", quietSignal: "σιωπή ή πολύ ασθενές σήμα",
                level: "Στάθμη μικροφώνου: {percent}%",
                languageError: "Δεν ήταν δυνατός ο συγχρονισμός της γλώσσας: {message}",
                insecure: "Αυτή η σελίδα ανοίχτηκε μέσω HTTP από άλλη συσκευή. Το Chrome για Android απαιτεί HTTPS για πρόσβαση στο μικρόφωνο. Ρυθμίστε ένα πιστοποιητικό που εμπιστεύεται το τηλέφωνο.",
                noMicrophoneApi: "Αυτή η ασφαλής προέλευση δεν παρέχει API μικροφώνου. Ελέγξτε τις ρυθμίσεις του προγράμματος περιήγησης και τα δικαιώματα του ιστοτόπου.",
                permission: "Αναμονή για άδεια χρήσης μικροφώνου...",
                connecting: "Επιτράπηκε η πρόσβαση στο μικρόφωνο, σύνδεση με τον υπολογιστή...",
                connected: "Συνδέθηκε! Μετάδοση ήχου σε εξέλιξη...",
                audioError: "Σφάλμα ήχου ({name}): {message}",
                websocketError: "Δεν ήταν δυνατή η σύνδεση στο WebSocket.",
                disconnectedAgain: "Αποσυνδέθηκε.",
                microphoneError: "Το μικρόφωνο δεν είναι διαθέσιμο ({name}): {message}"
            }
        };
        translations["de-at"] = { ...translations.de };
        const languageNames = {
            fr: "Français", en: "English", es: "Español", zh: "中文",
            ko: "한국어", ja: "日本語", pt: "Português", it: "Italiano",
            de: "Deutsch", "de-at": "Deutsch (Österreich)", nl: "Nederlands",
            hu: "Magyar", ro: "Română", pl: "Polski", ru: "Русский",
            uk: "Українська", lt: "Lietuvių", el: "Ελληνικά"
        };
        const settingsLabels = {
            fr: "Réglages", en: "Settings", es: "Ajustes", zh: "设置",
            ko: "설정", ja: "設定", pt: "Definições", it: "Impostazioni",
            de: "Einstellungen", "de-at": "Einstellungen", nl: "Instellingen",
            hu: "Beállítások", ro: "Setări", pl: "Ustawienia", ru: "Настройки",
            uk: "Налаштування", lt: "Nustatymai", el: "Ρυθμίσεις"
        };
        const speakerMonitorLabels = {
            fr: "Écouter également sur les haut-parleurs du PC",
            en: "Also play audio through the PC speakers",
            es: "Reproducir también el audio por los altavoces del PC",
            zh: "同时通过电脑扬声器播放音频",
            ko: "PC 스피커로도 오디오 재생",
            ja: "PC のスピーカーでも音声を再生する",
            pt: "Reproduzir também o áudio nos altifalantes do PC",
            it: "Riproduci l'audio anche dagli altoparlanti del PC",
            de: "Audio auch über die PC-Lautsprecher wiedergeben",
            "de-at": "Audio auch über die PC-Lautsprecher wiedergeben",
            nl: "Audio ook afspelen via de pc-luidsprekers",
            hu: "Hang lejátszása a számítógép hangszóróin is",
            ro: "Redă sunetul și prin difuzoarele PC-ului",
            pl: "Odtwarzaj dźwięk również przez głośniki komputera",
            ru: "Воспроизводить звук также через динамики ПК",
            uk: "Відтворювати звук також через динаміки ПК",
            lt: "Taip pat leisti garsą per kompiuterio garsiakalbius",
            el: "Αναπαραγωγή ήχου και από τα ηχεία του υπολογιστή"
        };
        const speakerMonitorErrors = {
            fr: "Impossible de modifier la sortie audio",
            en: "Could not change the audio output",
            es: "No se pudo cambiar la salida de audio",
            zh: "无法更改音频输出",
            ko: "오디오 출력을 변경할 수 없습니다",
            ja: "音声出力を変更できませんでした",
            pt: "Não foi possível alterar a saída de áudio",
            it: "Impossibile modificare l'uscita audio",
            de: "Audioausgabe konnte nicht geändert werden",
            "de-at": "Audioausgabe konnte nicht geändert werden",
            nl: "De audio-uitvoer kan niet worden gewijzigd",
            hu: "Nem sikerült módosítani a hangkimenetet",
            ro: "Ieșirea audio nu a putut fi schimbată",
            pl: "Nie można zmienić wyjścia audio",
            ru: "Не удалось изменить аудиовыход",
            uk: "Не вдалося змінити аудіовихід",
            lt: "Nepavyko pakeisti garso išvesties",
            el: "Δεν ήταν δυνατή η αλλαγή της εξόδου ήχου"
        };
        let currentLanguage = "en";
        let languageChangeRevision = 0;
        let currentStatus = { key: "disconnected", values: {} };
        let currentLinkStatus = { key: "linkWaiting", values: {} };

        const startBtn = document.getElementById('startBtn');
        const pairControls = document.getElementById('pairControls');
        const pairPin = document.getElementById('pairPin');
        const rePairBtn = document.getElementById('rePairBtn');
        const languageStatus = document.getElementById('languageStatus');
        const languageButtons = Array.from(
            document.querySelectorAll('#languageButtons button')
        );
        const status = document.getElementById('status');
        const linkBtn = document.getElementById('linkBtn');
        const linkStatus = document.getElementById('linkStatus');
        const levelText = document.getElementById('levelText');
        const levelMeter = document.getElementById('levelMeter');
        const settingsBtn = document.getElementById('settingsBtn');
        const settingsPanel = document.getElementById('settingsPanel');
        const darkModeToggle = document.getElementById('darkModeToggle');
        const speakerMonitor = document.getElementById('speakerMonitor');
        const speakerMonitorLabel = document.getElementById('speakerMonitorLabel');
        const speakerMonitorStatus = document.getElementById('speakerMonitorStatus');
        const themeColor = document.getElementById('themeColor');
        const DARK_MODE_STORAGE_KEY = 'miclonDarkMode';
        const noiseFilter = document.getElementById('noiseFilter');
        const noiseFilterValue = document.getElementById('noiseFilterValue');
        let noiseFilterStrength = Number(noiseFilter.value);
        noiseFilter.addEventListener('input', () => {
            noiseFilterStrength = Number(noiseFilter.value);
            noiseFilterValue.textContent = `${noiseFilterStrength} %`;
        });
        const pitchControl = document.getElementById('pitchControl');
        const pitchValue = document.getElementById('pitchValue');
        let pitchSemitones = Number(pitchControl.value);
        pitchControl.addEventListener('input', () => {
            pitchSemitones = Number(pitchControl.value);
            pitchValue.textContent =
                `${pitchSemitones > 0 ? "+" : ""}${pitchSemitones} st`;
        });
        const meterBars = Array.from({ length: 24 }, () => {
            const bar = document.createElement('span');
            bar.className = 'meter-bar';
            levelMeter.appendChild(bar);
            return bar;
        });
        let meterAnimationFrame;
        let currentAnalyser;
        let currentMeterSamples;
        let microphoneLinkEnabled = true;
        let speakerMonitorEnabled = false;
        let speakerMonitorSyncInProgress = false;
        let speakerMonitorToggleInProgress = false;
        let activeAudioTracks = [];
        let activeWebSocket = null;
        let linkSyncInProgress = false;
        let linkToggleInProgress = false;
        let linkStateRevision = 0;
        let pairingInProgress = false;
        let sessionToken = null;
        let sessionExpirationTimer;
        const SESSION_STORAGE_KEY = "miclonSession";
        const SESSION_DURATION_MS = 3 * 60 * 60 * 1000;

        function t(key, values = {}) {
            const locale = translations[currentLanguage] ?? translations.en;
            const fallback = translations.en ?? {};
            const text = String(locale[key] ?? fallback[key] ?? key);
            return text.replace(
                /\\{(\\w+)\\}/g,
                (match, name) => String(values[name] ?? match)
            );
        }

        function setStatus(key, values = {}) {
            currentStatus = { key, values };
            status.textContent = t(key, values);
        }

        function setLinkStatus(key, values = {}) {
            currentLinkStatus = { key, values };
            linkStatus.textContent = t(key, values);
        }

        function applyLanguage(language) {
            if (!Object.prototype.hasOwnProperty.call(translations, language)) {
                throw new Error(`Unsupported language: ${language}`);
            }
            currentLanguage = language;
            document.documentElement.lang = language;
            document.title = t("heading");
            document.getElementById("languagePickerLabel").textContent = t("language");
            document.querySelector(".language-picker").setAttribute(
                "aria-label", t("languageAria")
            );
            document.querySelectorAll("[data-i18n]").forEach(element => {
                element.textContent = t(element.dataset.i18n);
            });
            document.querySelectorAll("[data-i18n-aria]").forEach(element => {
                element.setAttribute("aria-label", t(element.dataset.i18nAria));
            });
            levelMeter.setAttribute("aria-label", t("meterLabel"));
            languageButtons.forEach(button => {
                const selected = button.dataset.language === language;
                button.setAttribute("aria-pressed", String(selected));
                button.title = languageNames[button.dataset.language];
                button.setAttribute("aria-label", languageNames[button.dataset.language]);
            });
            settingsBtn.title = settingsLabels[language];
            settingsBtn.setAttribute("aria-label", settingsLabels[language]);
            settingsPanel.setAttribute("aria-label", settingsLabels[language]);
            speakerMonitorLabel.textContent = speakerMonitorLabels[language];
            status.textContent = t(currentStatus.key, currentStatus.values);
            linkStatus.textContent = t(currentLinkStatus.key, currentLinkStatus.values);
            applyMicrophoneLinkState(microphoneLinkEnabled, false);
        }

        function applyDarkMode(enabled) {
            const shouldUseDarkMode = Boolean(enabled);
            document.body.classList.toggle("dark-mode", shouldUseDarkMode);
            darkModeToggle.checked = shouldUseDarkMode;
            localStorage.setItem(DARK_MODE_STORAGE_KEY, String(shouldUseDarkMode));
            themeColor.content = shouldUseDarkMode ? "#111111" : "#000000";
        }

        darkModeToggle.addEventListener('change', () => {
            applyDarkMode(darkModeToggle.checked);
        });

        async function changeLanguage(language) {
            languageChangeRevision++;
            applyLanguage(language);
            languageStatus.textContent = "";
            if (!sessionToken) {
                return;
            }
            languageButtons.forEach(button => {
                button.disabled = true;
            });
            try {
                const response = await fetch("/language", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "Authorization": `Bearer ${sessionToken}`
                    },
                    body: JSON.stringify({ language })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (state.language !== language) {
                    throw new Error("Server returned a different language");
                }
            } catch (error) {
                languageStatus.textContent = t("languageError", {
                    message: error.message
                });
                console.error("Could not sync language with the PC:", error);
            } finally {
                languageButtons.forEach(button => {
                    button.disabled = false;
                });
            }
        }

        async function loadLanguage() {
            const initialRevision = languageChangeRevision;
            try {
                const response = await fetch("/language", { cache: "no-store" });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (!Object.prototype.hasOwnProperty.call(
                    translations, state.language
                )) {
                    throw new Error("Server returned an unsupported language");
                }
                if (initialRevision === languageChangeRevision) {
                    applyLanguage(state.language);
                }
            } catch (error) {
                languageStatus.textContent = t("languageError", {
                    message: error.message
                });
                console.error("Could not load the language from the PC:", error);
            }
        }

        function activatePairedSession() {
            pairControls.hidden = true;
            startBtn.hidden = true;
            startBtn.disabled = false;
            rePairBtn.hidden = false;
            linkBtn.disabled = false;
            speakerMonitor.disabled = false;
            document.body.classList.add("paired");
            themeColor.content = document.body.classList.contains("dark-mode") ? "#111111" : "#000000";
            settingsPanel.hidden = true;
            settingsBtn.setAttribute("aria-expanded", "false");
            cancelAnimationFrame(meterAnimationFrame);
            setLinkStatus("paired");
        }

        function showMicrophoneStartButton() {
            if (!sessionToken) {
                return;
            }
            startBtn.hidden = false;
            startBtn.disabled = false;
            settingsPanel.hidden = false;
            settingsBtn.setAttribute("aria-expanded", "true");
        }

        function clearPairedSession() {
            window.clearTimeout(sessionExpirationTimer);
            sessionToken = null;
            localStorage.removeItem(SESSION_STORAGE_KEY);
            if (activeWebSocket) {
                activeWebSocket.close();
                activeWebSocket = null;
            }
            activeAudioTracks.forEach(track => track.stop());
            activeAudioTracks = [];
            document.body.classList.remove("paired");
            themeColor.content = document.body.classList.contains("dark-mode") ? "#111111" : "#000000";
            pairControls.hidden = false;
            startBtn.hidden = true;
            startBtn.disabled = true;
            rePairBtn.hidden = true;
            linkBtn.disabled = true;
            speakerMonitor.disabled = true;
            if (currentAnalyser && currentMeterSamples) {
                updateMicrophoneMeter(currentAnalyser, currentMeterSamples);
            }
            setLinkStatus("linkWaiting");
        }

        function scheduleSessionExpiration(expiresAt) {
            window.clearTimeout(sessionExpirationTimer);
            sessionExpirationTimer = window.setTimeout(
                clearPairedSession,
                Math.max(0, expiresAt - Date.now())
            );
        }

        async function restorePairedSession() {
            const storedSession = localStorage.getItem(SESSION_STORAGE_KEY);
            if (!storedSession) {
                return;
            }

            let savedSession;
            try {
                savedSession = JSON.parse(storedSession);
            } catch (error) {
                localStorage.removeItem(SESSION_STORAGE_KEY);
                return;
            }
            if (
                !savedSession
                || typeof savedSession.token !== "string"
                || typeof savedSession.expiresAt !== "number"
                || savedSession.expiresAt <= Date.now()
            ) {
                clearPairedSession();
                return;
            }

            sessionToken = savedSession.token;
            scheduleSessionExpiration(savedSession.expiresAt);
            activatePairedSession();
            startBtn.hidden = false;
            try {
                const response = await fetch("/microphone/status", {
                    cache: "no-store",
                    headers: { "Authorization": `Bearer ${sessionToken}` }
                });
                if (response.status === 401) {
                    clearPairedSession();
                    return;
                }
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (typeof state.enabled !== "boolean") {
                    throw new Error("État de liaison invalide");
                }
                applyMicrophoneLinkState(state.enabled);
                await syncSpeakerMonitorState();
            } catch (error) {
                setLinkStatus("linkReadError", { message: error.message });
                console.error("Impossible de restaurer la session :", error);
            }
        }

        languageButtons.forEach(button => {
            button.addEventListener("click", () => {
                changeLanguage(button.dataset.language);
            });
        });

        settingsBtn.addEventListener("click", () => {
            settingsPanel.hidden = !settingsPanel.hidden;
            settingsBtn.setAttribute("aria-expanded", String(!settingsPanel.hidden));
        });

        rePairBtn.addEventListener("click", () => {
            settingsPanel.hidden = true;
            settingsBtn.setAttribute("aria-expanded", "false");
            clearPairedSession();
        });

        async function pairDevice() {
            if (pairingInProgress || !/^\\d{8}$/.test(pairPin.value.trim())) {
                return;
            }
            pairingInProgress = true;
            pairPin.disabled = true;
            try {
                const response = await fetch("/pair", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ pin: pairPin.value.trim() })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const result = await response.json();
                if (typeof result.token !== "string") {
                    throw new Error("Jeton de session invalide");
                }
                const expiresAt = Date.now() + SESSION_DURATION_MS;
                localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
                    token: result.token,
                    expiresAt
                }));
                sessionToken = result.token;
                scheduleSessionExpiration(expiresAt);
                activatePairedSession();
                pairPin.value = "";
                void startMicrophone();
                await changeLanguage(currentLanguage);
                await syncMicrophoneLinkState();
                await syncSpeakerMonitorState();
            } catch (error) {
                setLinkStatus("pairError", { message: error.message });
                linkStatus.dataset.pairError = "true";
                console.error("Impossible d'associer cet appareil :", error);
            } finally {
                pairPin.disabled = false;
                pairingInProgress = false;
            }
        }

        pairPin.addEventListener("input", () => {
            delete linkStatus.dataset.pairError;
            if (pairPin.value.length === 8) {
                void pairDevice();
            }
        });

        pairPin.addEventListener("keydown", event => {
            if (event.key === "Enter") {
                event.preventDefault();
                void pairDevice();
            }
        });

        applyDarkMode(localStorage.getItem(DARK_MODE_STORAGE_KEY) === "true");
        applyLanguage(currentLanguage);
        loadLanguage();
        restorePairedSession();

        function applyMicrophoneLinkState(enabled, updateStatus = true) {
            microphoneLinkEnabled = enabled;
            activeAudioTracks.forEach(track => {
                track.enabled = enabled;
            });
            linkBtn.textContent = t(enabled ? "suspend" : "resume");
            linkBtn.classList.toggle("suspended", !enabled);
            linkBtn.setAttribute("aria-pressed", String(!enabled));
            if (updateStatus) {
                setLinkStatus(enabled ? "linkEnabled" : "linkSuspended");
            }
        }

        async function syncMicrophoneLinkState() {
            if (!sessionToken || linkSyncInProgress || linkToggleInProgress) {
                return;
            }
            linkSyncInProgress = true;
            const currentRevision = linkStateRevision;
            try {
                const response = await fetch("/microphone/status", {
                    cache: "no-store",
                    headers: { "Authorization": `Bearer ${sessionToken}` }
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (typeof state.enabled !== "boolean") {
                    throw new Error("État de liaison invalide");
                }
                if (currentRevision !== linkStateRevision) {
                    return;
                }
                applyMicrophoneLinkState(state.enabled);
            } catch (error) {
                setLinkStatus("linkReadError", { message: error.message });
                console.error("Impossible de lire l'état de la liaison micro :", error);
            } finally {
                linkSyncInProgress = false;
            }
        }

        function applySpeakerMonitorState(enabled) {
            speakerMonitorEnabled = enabled;
            speakerMonitor.checked = enabled;
        }

        async function syncSpeakerMonitorState() {
            if (
                !sessionToken
                || speakerMonitorSyncInProgress
                || speakerMonitorToggleInProgress
            ) {
                return;
            }
            speakerMonitorSyncInProgress = true;
            try {
                const response = await fetch("/speaker-monitor/status", {
                    cache: "no-store",
                    headers: { "Authorization": "Bearer " + sessionToken }
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (typeof state.enabled !== "boolean") {
                    throw new Error("État de sortie audio invalide");
                }
                applySpeakerMonitorState(state.enabled);
                speakerMonitorStatus.textContent = "";
            } catch (error) {
                speakerMonitorStatus.textContent =
                    `${speakerMonitorErrors[currentLanguage]} (${error.message})`;
                console.error("Impossible de lire l'état de la sortie audio :", error);
            } finally {
                speakerMonitorSyncInProgress = false;
            }
        }

        speakerMonitor.addEventListener("change", async () => {
            speakerMonitorToggleInProgress = true;
            speakerMonitor.disabled = true;
            try {
                const response = await fetch("/speaker-monitor", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + sessionToken
                    },
                    body: JSON.stringify({ enabled: speakerMonitor.checked })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (typeof state.enabled !== "boolean") {
                    throw new Error("État de sortie audio invalide");
                }
                applySpeakerMonitorState(state.enabled);
                speakerMonitorStatus.textContent = "";
            } catch (error) {
                speakerMonitor.checked = speakerMonitorEnabled;
                speakerMonitorStatus.textContent =
                    `${speakerMonitorErrors[currentLanguage]} (${error.message})`;
                console.error("Impossible de modifier la sortie audio :", error);
            } finally {
                speakerMonitorToggleInProgress = false;
                speakerMonitor.disabled = !sessionToken;
            }
        });

        linkBtn.addEventListener("click", async () => {
            linkBtn.disabled = true;
            linkToggleInProgress = true;
            linkStateRevision++;
            try {
                const response = await fetch("/microphone", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "Authorization": `Bearer ${sessionToken}`
                    },
                    body: JSON.stringify({ enabled: !microphoneLinkEnabled })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const state = await response.json();
                if (typeof state.enabled !== "boolean") {
                    throw new Error("État de liaison invalide");
                }
                applyMicrophoneLinkState(state.enabled);
                await logToServer("microphone_link_toggled", state);
            } catch (error) {
                setLinkStatus("linkWriteError", { message: error.message });
                console.error("Impossible de modifier la liaison micro :", error);
            } finally {
                linkToggleInProgress = false;
                linkBtn.disabled = false;
            }
        });
        syncMicrophoneLinkState();
        window.setInterval(syncMicrophoneLinkState, 1000);
        syncSpeakerMonitorState();
        window.setInterval(syncSpeakerMonitorState, 1000);

        function updateMicrophoneMeter(analyser, samples) {
            analyser.getByteTimeDomainData(samples);
            let sumSquares = 0;
            for (const sample of samples) {
                const amplitude = (sample - 128) / 128;
                sumSquares += amplitude * amplitude;
            }

            const rms = Math.sqrt(sumSquares / samples.length);
            const percent = Math.min(100, Math.round(rms * 1200));
            const activeBars = Math.round(percent / 100 * meterBars.length);
            meterBars.forEach((bar, index) => {
                const active = index < activeBars;
                bar.classList.toggle('active', active);
                bar.classList.toggle('warning', active && index >= 16);
                bar.classList.toggle('danger', active && index >= 21);
            });
            levelMeter.setAttribute('aria-valuenow', String(percent));
            levelText.textContent = `${t("level", { percent })} · ${
                t(rms > 0.008 ? "soundDetected" : "quietSignal")
            }`;
            if (!document.body.classList.contains("paired")) {
                meterAnimationFrame = requestAnimationFrame(
                    () => updateMicrophoneMeter(analyser, samples)
                );
            }
        }

        function browserInfo() {
            return {
                href: window.location.href,
                origin: window.location.origin,
                protocol: window.location.protocol,
                hostname: window.location.hostname,
                secureContext: window.isSecureContext,
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                language: navigator.language,
                online: navigator.onLine,
                visibilityState: document.visibilityState,
                mediaDevicesAvailable: Boolean(navigator.mediaDevices),
                getUserMediaAvailable: Boolean(navigator.mediaDevices?.getUserMedia)
            };
        }

        async function logToServer(event, details = {}) {
            const diagnostic = {
                event,
                timestamp: new Date().toISOString(),
                browser: browserInfo(),
                details
            };

            try {
                const response = await fetch("/diagnostics", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "Authorization": `Bearer ${sessionToken}`
                    },
                    body: JSON.stringify(diagnostic),
                    keepalive: true
                });
                if (!response.ok) {
                    console.error("Le serveur a refusé le diagnostic :", response.status,
                        await response.text());
                }
            } catch (error) {
                console.error("Impossible d'envoyer le diagnostic au serveur :", error);
            }
        }

        async function microphoneInfo() {
            const info = {};
            if (navigator.permissions?.query) {
                try {
                    const permission = await navigator.permissions.query({ name: "microphone" });
                    info.permissionState = permission.state;
                } catch (error) {
                    info.permissionQueryError = {
                        name: error.name,
                        message: error.message
                    };
                }
            } else {
                info.permissionsApiAvailable = false;
            }

            if (navigator.mediaDevices?.enumerateDevices) {
                try {
                    info.devices = (await navigator.mediaDevices.enumerateDevices())
                        .map(device => ({
                            kind: device.kind,
                            label: device.label,
                            deviceId: device.deviceId,
                            groupId: device.groupId
                        }));
                } catch (error) {
                    info.enumerateDevicesError = {
                        name: error.name,
                        message: error.message
                    };
                }
            }
            return info;
        }

        // Phase-vocoder processing shifts pitch while keeping each frame's duration.
        function createPitchShifter() {
            const frameSize = 2048;
            const hopSize = 512;
            const halfFrame = frameSize / 2;
            const binCount = halfFrame + 1;
            const bitReversal = new Uint16Array(frameSize);
            const twiddleCos = new Float32Array(halfFrame);
            const twiddleSin = new Float32Array(halfFrame);
            const window = new Float32Array(frameSize);
            for (let i = 0; i < frameSize; i++) {
                let reversed = 0;
                let value = i;
                for (let bit = 0; bit < 11; bit++) {
                    reversed = (reversed << 1) | (value & 1);
                    value >>= 1;
                }
                bitReversal[i] = reversed;
                window[i] = 0.5 - 0.5 * Math.cos(
                    2 * Math.PI * i / (frameSize - 1)
                );
            }
            for (let i = 0; i < halfFrame; i++) {
                const angle = 2 * Math.PI * i / frameSize;
                twiddleCos[i] = Math.cos(angle);
                twiddleSin[i] = Math.sin(angle);
            }

            const inputQueue = new Float32Array(frameSize * 2);
            const overlap = new Float32Array(frameSize);
            const overlapWeight = new Float32Array(frameSize);
            const fftReal = new Float32Array(frameSize);
            const fftImag = new Float32Array(frameSize);
            const magnitude = new Float32Array(binCount);
            const phase = new Float32Array(binCount);
            const frequency = new Float32Array(binCount);
            const previousPhase = new Float32Array(binCount);
            const synthesisPhase = new Float32Array(binCount);
            let inputLength = 0;
            let phaseInitialized = false;

            function fft(inverse) {
                for (let i = 0; i < frameSize; i++) {
                    const reversed = bitReversal[i];
                    if (reversed > i) {
                        let value = fftReal[i];
                        fftReal[i] = fftReal[reversed];
                        fftReal[reversed] = value;
                        value = fftImag[i];
                        fftImag[i] = fftImag[reversed];
                        fftImag[reversed] = value;
                    }
                }
                for (let size = 2; size <= frameSize; size *= 2) {
                    const halfSize = size / 2;
                    const twiddleStep = frameSize / size;
                    for (let start = 0; start < frameSize; start += size) {
                        for (let offset = 0; offset < halfSize; offset++) {
                            const twiddle = offset * twiddleStep;
                            const cosine = twiddleCos[twiddle];
                            const sine = twiddleSin[twiddle] * (inverse ? -1 : 1);
                            const even = start + offset;
                            const odd = even + halfSize;
                            const oddReal =
                                fftReal[odd] * cosine - fftImag[odd] * sine;
                            const oddImag =
                                fftReal[odd] * sine + fftImag[odd] * cosine;
                            fftReal[odd] = fftReal[even] - oddReal;
                            fftImag[odd] = fftImag[even] - oddImag;
                            fftReal[even] += oddReal;
                            fftImag[even] += oddImag;
                        }
                    }
                }
                if (inverse) {
                    for (let i = 0; i < frameSize; i++) {
                        fftReal[i] /= frameSize;
                        fftImag[i] /= frameSize;
                    }
                }
            }

            function processFrame(pitchFactor, output, outputOffset) {
                for (let i = 0; i < frameSize; i++) {
                    fftReal[i] = inputQueue[i] * window[i];
                    fftImag[i] = 0;
                }
                fft(false);

                for (let bin = 0; bin < binCount; bin++) {
                    const real = fftReal[bin];
                    const imaginary = fftImag[bin];
                    const currentPhase = Math.atan2(imaginary, real);
                    magnitude[bin] = Math.hypot(real, imaginary);
                    phase[bin] = currentPhase;
                    if (phaseInitialized) {
                        const expected = 2 * Math.PI * bin * hopSize / frameSize;
                        let deviation = currentPhase - previousPhase[bin] + expected;
                        deviation -= 2 * Math.PI * Math.round(
                            deviation / (2 * Math.PI)
                        );
                        frequency[bin] = (expected - deviation) / hopSize;
                    } else {
                        frequency[bin] = 2 * Math.PI * bin / frameSize;
                    }
                    previousPhase[bin] = currentPhase;
                }

                fftReal.fill(0);
                fftImag.fill(0);
                for (let bin = 0; bin < binCount; bin++) {
                    const sourcePosition = bin / pitchFactor;
                    if (sourcePosition > halfFrame) {
                        if (phaseInitialized) {
                            synthesisPhase[bin] -=
                                2 * Math.PI * bin * hopSize / frameSize;
                        }
                        continue;
                    }
                    const lower = Math.floor(sourcePosition);
                    const upper = Math.min(halfFrame, lower + 1);
                    const fraction = sourcePosition - lower;
                    const binMagnitude = magnitude[lower] * (1 - fraction)
                        + magnitude[upper] * fraction;
                    const binFrequency = frequency[lower] * (1 - fraction)
                        + frequency[upper] * fraction;
                    if (phaseInitialized) {
                        synthesisPhase[bin] -= binFrequency * pitchFactor * hopSize;
                    } else {
                        synthesisPhase[bin] = phase[lower] * (1 - fraction)
                            + phase[upper] * fraction;
                    }
                    fftReal[bin] = binMagnitude * Math.cos(synthesisPhase[bin]);
                    fftImag[bin] = binMagnitude * Math.sin(synthesisPhase[bin]);
                }
                phaseInitialized = true;
                for (let bin = 1; bin < halfFrame; bin++) {
                    fftReal[frameSize - bin] = fftReal[bin];
                    fftImag[frameSize - bin] = -fftImag[bin];
                }
                fft(true);
                for (let i = 0; i < frameSize; i++) {
                    const weighted = fftReal[i] * window[i];
                    overlap[i] += weighted;
                    overlapWeight[i] += window[i] * window[i];
                }
                for (let i = 0; i < hopSize; i++) {
                    output[outputOffset + i] = overlapWeight[i] > 1e-8
                        ? overlap[i] / overlapWeight[i]
                        : 0;
                }
                overlap.copyWithin(0, hopSize);
                overlap.fill(0, frameSize - hopSize);
                overlapWeight.copyWithin(0, hopSize);
                overlapWeight.fill(0, frameSize - hopSize);
            }

            return {
                process(samples, semitones) {
                    if (semitones === 0) {
                        inputLength = 0;
                        phaseInitialized = false;
                        overlap.fill(0);
                        overlapWeight.fill(0);
                        return samples;
                    }
                    inputQueue.set(samples, inputLength);
                    inputLength += samples.length;
                    const shifted = new Float32Array(samples.length);
                    const output = new Float32Array(
                        Math.ceil(inputLength / hopSize) * hopSize
                    );
                    let outputLength = 0;
                    const pitchFactor = Math.pow(2, semitones / 12);
                    while (inputLength >= frameSize) {
                        processFrame(pitchFactor, output, outputLength);
                        outputLength += hopSize;
                        inputQueue.copyWithin(0, hopSize, inputLength);
                        inputLength -= hopSize;
                    }
                    shifted.set(output.subarray(
                        0, Math.min(outputLength, shifted.length)
                    ));
                    return shifted;
                }
            };
        }

        async function startMicrophone() {
            startBtn.hidden = true;
            startBtn.disabled = true;
            const initialInfo = await microphoneInfo();
            await logToServer("microphone_button_clicked", initialInfo);
            if (!window.isSecureContext) {
                const reason = t("insecure");
                setStatus("insecure");
                await logToServer("microphone_api_unavailable", {
                    reason,
                    ...initialInfo
                });
                showMicrophoneStartButton();
                return;
            }
            if (!navigator.mediaDevices?.getUserMedia) {
                const reason = t("noMicrophoneApi");
                setStatus("noMicrophoneApi");
                await logToServer("microphone_api_unavailable", {
                    reason,
                    ...initialInfo
                });
                showMicrophoneStartButton();
                return;
            }

            let mediaStream;
            try {
                setStatus("permission");
                await logToServer("get_user_media_requested", {
                    constraints: {
                        audio: {
                            echoCancellation: false,
                            noiseSuppression: false,
                            autoGainControl: false,
                            sampleRate: 44100
                        }
                    },
                    ...initialInfo
                });
                mediaStream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: false,
                        noiseSuppression: false,
                        autoGainControl: false,
                        sampleRate: 44100
                    }
                });
                activeAudioTracks = mediaStream.getAudioTracks();
                activeAudioTracks.forEach(track => {
                    track.enabled = microphoneLinkEnabled;
                });

                const audioTracks = mediaStream.getAudioTracks().map(track => {
                    let capabilities = {};
                    try {
                        capabilities = track.getCapabilities();
                    } catch (error) {
                        capabilities = { error: `${error.name}: ${error.message}` };
                    }
                    return {
                        id: track.id,
                        label: track.label,
                        enabled: track.enabled,
                        muted: track.muted,
                        readyState: track.readyState,
                        settings: track.getSettings(),
                        constraints: track.getConstraints(),
                        capabilities
                    };
                });
                await logToServer("get_user_media_succeeded", {
                    streamActive: mediaStream.active,
                    audioTracks,
                    ...await microphoneInfo()
                });
                const audioContext = new AudioContext({ sampleRate: 44100 });
                await audioContext.resume();
                const source = audioContext.createMediaStreamSource(mediaStream);
                const analyser = audioContext.createAnalyser();
                analyser.fftSize = 256;
                source.connect(analyser);
                const meterSamples = new Uint8Array(analyser.fftSize);
                currentAnalyser = analyser;
                currentMeterSamples = meterSamples;
                updateMicrophoneMeter(analyser, meterSamples);
                await logToServer("audio_meter_started", {
                    state: audioContext.state,
                    sampleRate: audioContext.sampleRate,
                    analyserFftSize: analyser.fftSize
                });
                setStatus("connecting");
                const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
                const wsUrl = `${wsProtocol}//${window.location.hostname}:5005`;
                await logToServer("websocket_connecting", { wsUrl });
                const ws = new WebSocket(wsUrl);
                activeWebSocket = ws;

                ws.onopen = async () => {
                    startBtn.hidden = true;
                    ws.send(JSON.stringify({
                        type: "auth",
                        token: sessionToken
                    }));
                    await logToServer("websocket_open", {
                        wsUrl,
                        readyState: ws.readyState,
                        audioContextSampleRate: 44100
                    });
                    setStatus("connected");
                    try {
                        const pitchShifter = createPitchShifter();
                        const processor = audioContext.createScriptProcessor(1024, 1, 1);
                        let sentFrames = 0;
                        let sentBytes = 0;
                        let filterGain = 1;

                        source.connect(processor);
                        processor.connect(audioContext.destination);
                        await logToServer("audio_context_started", {
                            state: audioContext.state,
                            sampleRate: audioContext.sampleRate,
                            channelCount: mediaStream.getAudioTracks().length
                        });

                        processor.onaudioprocess = (e) => {
                            const inputData = e.inputBuffer.getChannelData(0);
                            const filteredData = new Float32Array(inputData.length);
                            let rms = 0;
                            if (noiseFilterStrength > 0) {
                                let sumSquares = 0;
                                for (const sample of inputData) {
                                    sumSquares += sample * sample;
                                }
                                rms = Math.sqrt(sumSquares / inputData.length);
                            }
                            const threshold = 0.001 * Math.pow(
                                20,
                                noiseFilterStrength / 100
                            );
                            const targetGain = noiseFilterStrength === 0
                                ? 1
                                : Math.max(
                                    0.03,
                                    Math.min(1, (rms - threshold * 0.5) / threshold)
                                );
                            for (let i = 0; i < inputData.length; i++) {
                                const smoothing = targetGain > filterGain ? 0.02 : 0.002;
                                filterGain += (targetGain - filterGain) * smoothing;
                                const filteredSample = inputData[i] * filterGain;
                                filteredData[i] = filteredSample;
                            }
                            const shiftedData = pitchShifter.process(
                                filteredData,
                                pitchSemitones
                            );
                            const pcmData = new Int16Array(shiftedData.length);
                            for (let i = 0; i < shiftedData.length; i++) {
                                pcmData[i] = Math.round(
                                    Math.max(-1, Math.min(1, shiftedData[i])) * 0x7FFF
                                );
                            }
                            if (ws.readyState === WebSocket.OPEN) {
                                ws.send(pcmData.buffer);
                                sentFrames++;
                                sentBytes += pcmData.byteLength;
                                if (sentFrames === 1 || sentFrames % 500 === 0) {
                                    logToServer("audio_transmission_progress", {
                                        sentFrames,
                                        sentBytes,
                                        lastFrameSamples: inputData.length,
                                        websocketBufferedAmount: ws.bufferedAmount
                                    });
                                }
                            }
                        };
                    } catch (error) {
                        setStatus("audioError", {
                            name: error.name,
                            message: error.message
                        });
                        await logToServer("audio_context_error", {
                            name: error.name,
                            message: error.message,
                            stack: error.stack
                        });
                        showMicrophoneStartButton();
                    }
                };

                ws.onerror = async (event) => {
                    setStatus("websocketError");
                    await logToServer("websocket_error", {
                        wsUrl,
                        readyState: ws.readyState,
                        bufferedAmount: ws.bufferedAmount,
                        eventType: event.type
                    });
                    mediaStream.getTracks().forEach(track => track.stop());
                    cancelAnimationFrame(meterAnimationFrame);
                    if (activeWebSocket === ws) {
                        activeWebSocket = null;
                    }
                    showMicrophoneStartButton();
                };
                ws.onclose = async (event) => {
                    setStatus("disconnectedAgain");
                    await logToServer("websocket_closed", {
                        wsUrl,
                        code: event.code,
                        reason: event.reason,
                        wasClean: event.wasClean
                    });
                    mediaStream.getTracks().forEach(track => track.stop());
                    cancelAnimationFrame(meterAnimationFrame);
                    if (activeWebSocket === ws) {
                        activeWebSocket = null;
                    }
                    showMicrophoneStartButton();
                };
            } catch (error) {
                setStatus("microphoneError", {
                    name: error.name,
                    message: error.message
                });
                if (meterAnimationFrame) {
                    cancelAnimationFrame(meterAnimationFrame);
                }
                await logToServer("get_user_media_error", {
                    name: error.name,
                    message: error.message,
                    constraint: error.constraint,
                    stack: error.stack,
                    ...await microphoneInfo()
                });
                showMicrophoneStartButton();
            }
        }
        startBtn.addEventListener("click", startMicrophone);

        const phoneAdConfig = __PHONE_AD_CONFIG__;
        const phoneAdSlot = document.getElementById("phoneAdSlot");
        const phoneAdContent = document.getElementById("phoneAdContent");
        if (phoneAdConfig.enabled) {
            phoneAdSlot.hidden = false;
            if (!["gif", "partner", "rss"].includes(phoneAdConfig.mode)) {
                phoneAdContent.textContent =
                    'PHONE_AD_MODE doit être "gif", "partner" ou "rss".';
            } else if (!phoneAdConfig.url) {
                phoneAdContent.textContent =
                    "Configurez PHONE_AD_URL avec une adresse HTTPS.";
            } else {
                let adUrl;
                try {
                    adUrl = new URL(phoneAdConfig.url, window.location.href);
                } catch {
                    phoneAdContent.textContent =
                        "PHONE_AD_URL doit être une adresse ou un chemin HTTPS valide.";
                }
                if (adUrl && phoneAdConfig.mode === "rss") {
                    try {
                        if (new URL(phoneAdConfig.url).protocol !== "https:") {
                            throw new Error("URL RSS non HTTPS");
                        }
                    } catch {
                        phoneAdContent.textContent =
                            "Pour le mode RSS, PHONE_AD_URL doit être une URL HTTPS absolue.";
                        adUrl = null;
                    }
                }
                if (adUrl && adUrl.protocol !== "https:") {
                    phoneAdContent.textContent =
                        "PHONE_AD_URL doit utiliser HTTPS.";
                    adUrl = null;
                }
                if (adUrl && phoneAdConfig.mode === "gif") {
                    const image = document.createElement("img");
                    image.src = adUrl.href;
                    image.alt = phoneAdConfig.alt;
                    image.referrerPolicy = "no-referrer";
                    image.addEventListener("error", () => {
                        phoneAdContent.textContent =
                            "Impossible de charger le GIF configuré.";
                    });
                    phoneAdContent.appendChild(image);
                } else if (adUrl && phoneAdConfig.mode === "partner") {
                    const frame = document.createElement("iframe");
                    frame.src = adUrl.href;
                    frame.title = phoneAdConfig.alt;
                    frame.loading = "lazy";
                    frame.referrerPolicy = "no-referrer";
                    frame.sandbox =
                        "allow-scripts allow-forms allow-popups " +
                        "allow-popups-to-escape-sandbox";
                    phoneAdContent.appendChild(frame);
                } else if (adUrl && phoneAdConfig.mode === "rss") {
                    fetch("/rss-feed", { cache: "no-store" })
                        .then(response => {
                            if (!response.ok) {
                                throw new Error(`HTTP ${response.status}`);
                            }
                            return response.json();
                        })
                        .then(feed => {
                            const container = document.createElement("div");
                            container.className = "rss-feed";
                            const title = document.createElement("h2");
                            title.textContent = feed.title;
                            container.appendChild(title);
                            const list = document.createElement("ul");
                            feed.items.forEach(item => {
                                const listItem = document.createElement("li");
                                if (item.link) {
                                    const link = document.createElement("a");
                                    link.href = item.link;
                                    link.target = "_blank";
                                    link.rel = "noopener noreferrer";
                                    link.textContent = item.title;
                                    listItem.appendChild(link);
                                } else {
                                    listItem.textContent = item.title;
                                }
                                if (Array.isArray(item.images)) {
                                    item.images.forEach(imageUrl => {
                                        const image = document.createElement("img");
                                        image.src = imageUrl;
                                        image.alt = item.title;
                                        image.loading = "lazy";
                                        image.referrerPolicy = "no-referrer";
                                        image.addEventListener("error", () => image.remove());
                                        listItem.appendChild(image);
                                    });
                                }
                                list.appendChild(listItem);
                            });
                            container.appendChild(list);
                            phoneAdContent.replaceChildren(container);
                        })
                        .catch(error => {
                            console.error("Impossible de charger le flux RSS.", error);
                            phoneAdContent.textContent =
                                "Impossible de charger le flux RSS configuré.";
                        });
                }
            }
        }
    </script>
</body>
</html>
"""

# Serveur HTTP simple pour distribuer la page web au téléphone
def application_file(filename):
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent / filename

    executable_directory = Path(sys.executable).resolve().parent
    external_file = executable_directory / filename
    if external_file.is_file():
        return external_file

    bundle_directory = Path(getattr(sys, "_MEIPASS", executable_directory))
    return bundle_directory / filename


class TinyHTTPHandler(http.server.BaseHTTPRequestHandler):
    def is_authenticated(self):
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        return (
            scheme.lower() == "bearer"
            and bool(separator)
            and is_valid_session_token(token)
        )

    def verify_pairing_pin(self, pin):
        remote_ip = self.client_address[0]
        now = time.monotonic()
        with pairing_attempts_lock:
            failures, locked_until = pairing_attempts.get(remote_ip, (0, 0))
            if now < locked_until:
                return False
            if locked_until:
                failures = 0
                locked_until = 0

            if (
                isinstance(pin, str)
                and len(pin) == 8
                and pin.isascii()
                and pin.isdigit()
                and secrets.compare_digest(pin, PAIRING_PIN)
            ):
                pairing_attempts.pop(remote_ip, None)
                return True

            failures += 1
            if failures >= PAIRING_ATTEMPT_LIMIT:
                locked_until = now + PAIRING_LOCKOUT_SECONDS
                failures = 0
            pairing_attempts[remote_ip] = (failures, locked_until)
            return False

    def do_GET(self):
        if self.path == "/pub.html":
            ad_path = application_file("pub.html")
            try:
                payload = ad_path.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "Fichier pub.html introuvable")
                return
            except OSError:
                self.send_error(500, "Impossible de lire pub.html")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/publicite.gif":
            gif_path = application_file("publicite.gif")
            try:
                payload = gif_path.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "Fichier publicite.gif introuvable")
                return
            except OSError:
                self.send_error(500, "Impossible de lire publicite.gif")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/rss-feed":
            try:
                payload = json.dumps(load_rss_feed()).encode("utf-8")
            except (
                OSError,
                ValueError,
                ET.ParseError,
                urllib.error.URLError,
            ):
                self.send_error(502, "Impossible de charger le flux RSS")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/language":
            payload = json.dumps(
                {"language": get_current_language()}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/microphone/status":
            if not self.is_authenticated():
                self.send_error(401, "Authentification requise")
                return
            payload = json.dumps(
                {"enabled": get_microphone_link_enabled()}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/speaker-monitor/status":
            if not self.is_authenticated():
                self.send_error(401, "Authentification requise")
                return
            payload = json.dumps(
                {"enabled": get_speaker_monitor_enabled()}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        ad_config = json.dumps(
            {
                "enabled": PHONE_AD_ENABLED,
                "mode": PHONE_AD_MODE,
                "url": PHONE_AD_URL,
                "alt": PHONE_AD_ALT,
            }
        ).replace("<", "\\u003c")
        html_content = HTML_CONTENT.replace("__PHONE_AD_CONFIG__", ad_config)
        self.wfile.write(html_content.encode("utf-8"))

    def do_POST(self):
        if self.path == "/language":
            if not self.is_authenticated():
                self.send_error(401, "Authentification requise")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Taille de requête invalide")
                return
            if content_length < 0:
                self.send_error(400, "Taille de requête invalide")
                return
            if content_length > 1024:
                self.send_error(413, "Requête trop volumineuse")
                return
            try:
                payload = json.loads(
                    self.rfile.read(content_length).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400, "Requête JSON invalide")
                return
            language = payload.get("language") if isinstance(payload, dict) else None
            if not isinstance(language, str) or language not in LANGUAGE_NAMES:
                self.send_error(400, "Langue non prise en charge")
                return

            set_current_language(language)
            response = json.dumps(
                {"language": get_current_language()}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if self.path == "/pair":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Taille de requête invalide")
                return
            if content_length < 0 or content_length > 1024:
                self.send_error(413, "Requête trop volumineuse")
                return
            try:
                payload = json.loads(
                    self.rfile.read(content_length).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400, "Requête JSON invalide")
                return
            pin = payload.get("pin") if isinstance(payload, dict) else None
            if not self.verify_pairing_pin(pin):
                self.send_error(401, "Code PIN invalide ou temporairement bloqué")
                return

            global session_token_expires_at
            with session_token_lock:
                session_token_expires_at = (
                    time.monotonic() + SESSION_DURATION_SECONDS
                )
            response = json.dumps({"token": SESSION_TOKEN}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if self.path == "/speaker-monitor":
            if not self.is_authenticated():
                self.send_error(401, "Authentification requise")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Taille de requête invalide")
                return
            if content_length < 0 or content_length > 1024:
                self.send_error(413, "Commande trop volumineuse")
                return

            try:
                payload = json.loads(
                    self.rfile.read(content_length).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400, "Commande JSON invalide")
                return
            if (
                not isinstance(payload, dict)
                or type(payload.get("enabled")) is not bool
            ):
                self.send_error(400, "Le champ enabled doit être booléen")
                return

            try:
                set_speaker_monitor_enabled(payload["enabled"])
            except Exception:
                self.send_error(503, "Impossible de configurer la sortie haut-parleur")
                return

            state = json.dumps(
                {"enabled": get_speaker_monitor_enabled()}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(state)))
            self.end_headers()
            self.wfile.write(state)
            return

        if self.path == "/microphone":
            if not self.is_authenticated():
                self.send_error(401, "Authentification requise")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Taille de requête invalide")
                return
            if content_length > 1024:
                self.send_error(413, "Commande trop volumineuse")
                return

            try:
                payload = json.loads(
                    self.rfile.read(content_length).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400, "Commande JSON invalide")
                return
            if (
                not isinstance(payload, dict)
                or type(payload.get("enabled")) is not bool
            ):
                self.send_error(400, "Le champ enabled doit être booléen")
                return

            set_microphone_link_enabled(payload["enabled"])
            state = json.dumps(
                {"enabled": get_microphone_link_enabled()}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(state)))
            self.end_headers()
            self.wfile.write(state)
            return

        if self.path != "/diagnostics":
            self.send_error(404, "Endpoint inconnu")
            return
        if not self.is_authenticated():
            self.send_error(401, "Authentification requise")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > 65536:
            self.send_error(413, "Diagnostic trop volumineux")
            return

        try:
            diagnostic = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(diagnostic, dict):
                raise ValueError("Le diagnostic doit être un objet JSON")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.send_error(400, "Diagnostic JSON invalide")
            return

        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        pass


class QuietThreadingTCPServer(socketserver.ThreadingTCPServer):
    def handle_error(self, request, client_address):
        pass

def create_tls_context(lan_ip):
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject_alt_names = [x509.DNSName("localhost")]
    if lan_ip != "localhost":
        subject_alt_names.insert(
            0,
            x509.IPAddress(ipaddress.ip_address(lan_ip)),
        )
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "MICLON local server")
    ])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=14))
        .add_extension(
            x509.SubjectAlternativeName(subject_alt_names),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        with tempfile.TemporaryDirectory(prefix="miclon-tls-") as directory:
            certificate_file = Path(directory) / "certificate.pem"
            private_key_file = Path(directory) / "private-key.pem"
            certificate_file.write_bytes(certificate_pem)
            private_key_file.write_bytes(private_key_pem)
            tls_context.load_cert_chain(
                str(certificate_file),
                str(private_key_file),
            )
    except (OSError, ssl.SSLError) as error:
        raise RuntimeError(
            "Impossible de préparer le certificat HTTPS auto-signé."
        ) from error
    return tls_context


def start_http_server(tls_context=None):
    with QuietThreadingTCPServer(
        ("0.0.0.0", 8080), TinyHTTPHandler
    ) as httpd:
        if tls_context is not None:
            httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
        httpd.serve_forever()


def print_qr_code(url):
    qr_code = cv2.QRCodeEncoder_create().encode(url)
    qr_code = cv2.copyMakeBorder(
        qr_code,
        4,
        4,
        4,
        4,
        cv2.BORDER_CONSTANT,
        value=255,
    )
    print("[QR] " + pc_text("qr_instructions"))
    for row_index in range(0, qr_code.shape[0], 2):
        top_row = qr_code[row_index]
        bottom_row = (
            qr_code[row_index + 1]
            if row_index + 1 < qr_code.shape[0]
            else [255] * qr_code.shape[1]
        )
        line = []
        for top_pixel, bottom_pixel in zip(top_row, bottom_row):
            foreground = "\x1b[30m" if top_pixel < 128 else "\x1b[37m"
            background = "\x1b[40m" if bottom_pixel < 128 else "\x1b[47m"
            line.append(f"{foreground}{background}▀")
        print("".join(line) + "\x1b[0m")


# Récepteur WebSocket pour traiter le flux audio
async def audio_receiver(websocket):
    try:
        try:
            auth_message = await asyncio.wait_for(websocket.recv(), timeout=10)
        except asyncio.TimeoutError:
            await websocket.close(code=1008, reason="Authentification requise")
            return
        if not isinstance(auth_message, str):
            await websocket.close(code=1008, reason="Authentification requise")
            return
        try:
            auth_payload = json.loads(auth_message)
        except json.JSONDecodeError:
            auth_payload = None
        token = (
            auth_payload.get("token")
            if isinstance(auth_payload, dict)
            and auth_payload.get("type") == "auth"
            else None
        )
        if (
            not is_valid_session_token(token)
        ):
            await websocket.close(code=1008, reason="Authentification requise")
            return

        async for message in websocket:
            if not is_valid_session_token(token):
                await websocket.close(
                    code=1008,
                    reason="Session expirée"
                )
                return
            if not isinstance(message, bytes):
                await websocket.close(code=1008, reason="Trame audio invalide")
                return
            message_size = len(message)
            audio_data = (
                bytes(message)
                if get_microphone_link_enabled()
                else bytes(message_size)
            )
            write_audio(audio_data)
    except websockets.exceptions.ConnectionClosed:
        return

async def main():
    # Trouver l'IP locale du PC pour l'afficher à l'utilisateur
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
    except OSError:
        ip = "localhost"
    finally: s.close()

    tls_context = create_tls_context(ip)

    # Lance le serveur web dans un thread séparé.
    threading.Thread(
        target=start_http_server,
        args=(tls_context,),
        daemon=True,
    ).start()

    # Le WebSocket utilise le même certificat que la page web.
    async with websockets.serve(
        audio_receiver,
        "0.0.0.0",
        5005,
        max_size=CHUNK * 2,
        ssl=tls_context,
        logger=WEBSOCKET_LOGGER,
    ):
        print("[PAIRING] " + pc_text("pairing_pin", pin=PAIRING_PIN))
        protocol = "https" if tls_context is not None else "http"
        phone_url = f"{protocol}://{ip}:8080"
        print("1. " + pc_text("phone_url", url=phone_url))
        print_qr_code(phone_url)
        print("[HTTPS] " + pc_text("certificate_warning"))
        print("2. " + pc_text("start_microphone"))
        print("[KEYBOARD] " + pc_text("escape_to_quit"))

        stop_event = asyncio.Event()
        listener_stop = threading.Event()
        loop = asyncio.get_running_loop()

        def listen_for_escape():
            while not listener_stop.wait(0.05):
                if msvcrt.kbhit() and msvcrt.getwch() == "\x1b":
                    loop.call_soon_threadsafe(stop_event.set)
                    return

        keyboard_listener = threading.Thread(
            target=listen_for_escape,
            daemon=True,
        )
        keyboard_listener.start()
        try:
            await stop_event.wait()
        finally:
            listener_stop.set()
            keyboard_listener.join(timeout=0.2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            with speaker_monitor_lock:
                if speaker_monitor_stream is not None:
                    speaker_monitor_stream.stop_stream()
                    speaker_monitor_stream.close()
                    speaker_monitor_stream = None
                    speaker_monitor_enabled = False
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()
