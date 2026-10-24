import asyncio
import http.server
import json
import msvcrt
import os
import ssl
import socketserver
import threading
import cv2
import pyaudio
import websockets

# --- CONFIGURATION AUDIO ---
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100
CHUNK = 1024

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
            f"Sortie VB-CABLE introuvable. Sorties audio disponibles : {outputs}"
        )

    device_index, device_name = matches[0]
    print(f"Sortie audio sélectionnée : {device_name}")
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
    <meta id="themeColor" name="theme-color" content="#f0f0f0">
    <style>
        body { font-family: sans-serif; text-align: center; padding: 50px; background: #f0f0f0; }
        button { padding: 20px 40px; font-size: 20px; cursor: pointer; border-radius: 10px; border: none; background: #007bff; color: white; }
        button:disabled { background: #ccc; }
        .meter { display: flex; justify-content: center; gap: 4px; max-width: 420px; margin: 12px auto; }
        .meter-bar { height: 24px; flex: 1; border-radius: 3px; background: #d0d0d0; }
        .meter-bar.active { background: #28a745; }
        .meter-bar.active.warning { background: #ffc107; }
        .meter-bar.active.danger { background: #dc3545; }
        #levelText { font-variant-numeric: tabular-nums; }
        .filter-control { max-width: 420px; margin: 24px auto; text-align: left; }
        .filter-control label { display: flex; justify-content: space-between; gap: 12px; }
        .filter-control input { width: 100%; margin: 12px 0; }
        .filter-help { color: #555; font-size: 14px; }
        #linkBtn { background: #dc3545; }
        #linkBtn.suspended { background: #28a745; }
        body.eco-mode { background: #000; color: #181818; padding: 12px; }
        body.eco-mode h1,
        body.eco-mode #status,
        body.eco-mode #levelText { color: #181818; }
        body.eco-mode button { background: #080808; color: #303030; }
        body.eco-mode button:disabled { background: #050505; color: #151515; }
        body.eco-mode .meter,
        body.eco-mode .filter-control,
        body.eco-mode #levelText { display: none; }
        body.eco-mode #ecoModeBtn {
            position: fixed;
            top: 8px;
            right: 8px;
            padding: 10px 12px;
            font-size: 14px;
            font-weight: bold;
            background: #007bff;
            color: white;
        }
        body.eco-mode #linkBtn {
            position: fixed;
            bottom: 8px;
            left: 8px;
            right: 8px;
            padding: 12px;
            font-size: 14px;
            background: #dc3545;
            color: white;
        }
        body.eco-mode #linkBtn.suspended { background: #28a745; }
    </style>
</head>
<body>
    <h1>Microphone Externe</h1>
    <button id="startBtn">Démarrer le Micro</button>
    <button id="linkBtn" type="button" aria-pressed="false">
        Suspendre la liaison micro
    </button>
    <button id="ecoModeBtn" type="button" aria-pressed="false">Activer le mode Éco</button>
    <p id="status">Déconnecté</p>
    <p id="linkStatus" role="status">État de la liaison en attente...</p>
    <p id="levelText">Niveau du micro : en attente</p>
    <div id="levelMeter" class="meter" role="meter" aria-label="Niveau sonore du microphone"
         aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"></div>
    <div class="filter-control">
        <label for="noiseFilter">
            <span>Filtre antiparasite</span>
            <span id="noiseFilterValue">35 %</span>
        </label>
        <input id="noiseFilter" type="range" min="0" max="100" value="35"
               aria-label="Intensité du filtre antiparasite">
        <p class="filter-help">
            Réduit le bruit de fond pendant les silences. Une intensité trop forte
            peut aussi atténuer les voix faibles. Réglez à 0 pour désactiver le filtre.
        </p>
    </div>

    <script>
        const startBtn = document.getElementById('startBtn');
        const status = document.getElementById('status');
        const linkBtn = document.getElementById('linkBtn');
        const linkStatus = document.getElementById('linkStatus');
        const levelText = document.getElementById('levelText');
        const levelMeter = document.getElementById('levelMeter');
        const ecoModeBtn = document.getElementById('ecoModeBtn');
        const themeColor = document.getElementById('themeColor');
        const noiseFilter = document.getElementById('noiseFilter');
        const noiseFilterValue = document.getElementById('noiseFilterValue');
        let noiseFilterStrength = Number(noiseFilter.value);
        noiseFilter.addEventListener('input', () => {
            noiseFilterStrength = Number(noiseFilter.value);
            noiseFilterValue.textContent = `${noiseFilterStrength} %`;
        });
        const meterBars = Array.from({ length: 24 }, () => {
            const bar = document.createElement('span');
            bar.className = 'meter-bar';
            levelMeter.appendChild(bar);
            return bar;
        });
        let meterAnimationFrame;
        let ecoMode = false;
        let currentAnalyser;
        let currentMeterSamples;
        let microphoneLinkEnabled = true;
        let activeAudioTracks = [];
        let linkSyncInProgress = false;
        let linkToggleInProgress = false;
        let linkStateRevision = 0;

        function applyMicrophoneLinkState(enabled) {
            microphoneLinkEnabled = enabled;
            activeAudioTracks.forEach(track => {
                track.enabled = enabled;
            });
            linkBtn.textContent = enabled
                ? "Suspendre la liaison micro"
                : "Reprendre la liaison micro";
            linkBtn.classList.toggle("suspended", !enabled);
            linkBtn.setAttribute("aria-pressed", String(!enabled));
            linkStatus.textContent = enabled
                ? "Liaison micro activée"
                : "Liaison micro suspendue";
        }

        async function syncMicrophoneLinkState() {
            if (linkSyncInProgress || linkToggleInProgress) {
                return;
            }
            linkSyncInProgress = true;
            const currentRevision = linkStateRevision;
            try {
                const response = await fetch("/microphone/status", {
                    cache: "no-store"
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
                linkStatus.textContent =
                    `Impossible de lire l'état de la liaison : ${error.message}`;
                console.error("Impossible de lire l'état de la liaison micro :", error);
            } finally {
                linkSyncInProgress = false;
            }
        }

        linkBtn.addEventListener("click", async () => {
            linkBtn.disabled = true;
            linkToggleInProgress = true;
            linkStateRevision++;
            try {
                const response = await fetch("/microphone", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
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
                linkStatus.textContent =
                    `Impossible de modifier la liaison : ${error.message}`;
                console.error("Impossible de modifier la liaison micro :", error);
            } finally {
                linkToggleInProgress = false;
                linkBtn.disabled = false;
            }
        });
        syncMicrophoneLinkState();
        window.setInterval(syncMicrophoneLinkState, 1000);

        ecoModeBtn.addEventListener('click', () => {
            ecoMode = !ecoMode;
            document.body.classList.toggle('eco-mode', ecoMode);
            themeColor.content = ecoMode ? '#000000' : '#f0f0f0';
            ecoModeBtn.setAttribute('aria-pressed', String(ecoMode));
            ecoModeBtn.textContent = ecoMode
                ? 'Revenir au mode classique'
                : 'Activer le mode Éco';
            if (ecoMode) {
                cancelAnimationFrame(meterAnimationFrame);
            } else if (currentAnalyser && currentMeterSamples) {
                updateMicrophoneMeter(currentAnalyser, currentMeterSamples);
            }
        });

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
            levelText.textContent = `Niveau du micro : ${percent}% · ${
                rms > 0.008 ? 'son détecté' : 'silence ou signal très faible'
            }`;
            if (!ecoMode) {
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
                    headers: { "Content-Type": "application/json" },
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

        startBtn.onclick = async () => {
            startBtn.disabled = true;
            const initialInfo = await microphoneInfo();
            await logToServer("microphone_button_clicked", initialInfo);
            if (!window.isSecureContext) {
                const reason = "Cette page est ouverte en HTTP depuis un autre appareil. "
                    + "Chrome Android exige HTTPS pour accéder au microphone. "
                    + "Configurez un certificat approuvé par le téléphone.";
                status.innerText = reason;
                await logToServer("microphone_api_unavailable", {
                    reason,
                    ...initialInfo
                });
                startBtn.disabled = false;
                return;
            }
            if (!navigator.mediaDevices?.getUserMedia) {
                const reason = "Cette origine sécurisée ne fournit pas l'API microphone. "
                    + "Vérifiez les paramètres du navigateur et les autorisations du site.";
                status.innerText = reason;
                await logToServer("microphone_api_unavailable", {
                    reason,
                    ...initialInfo
                });
                startBtn.disabled = false;
                return;
            }

            let mediaStream;
            try {
                status.innerText = "Autorisation du microphone...";
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
                status.innerText = "Micro autorisé, connexion au PC...";
                const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
                const wsUrl = `${wsProtocol}//${window.location.hostname}:5005`;
                await logToServer("websocket_connecting", { wsUrl });
                const ws = new WebSocket(wsUrl);

                ws.onopen = async () => {
                    await logToServer("websocket_open", {
                        wsUrl,
                        readyState: ws.readyState,
                        audioContextSampleRate: 44100
                    });
                    status.innerText = "Connecté ! Transmission audio en cours...";
                    try {
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
                            const pcmData = new Int16Array(inputData.length);
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
                                pcmData[i] = Math.round(
                                    Math.max(-1, Math.min(1, filteredSample)) * 0x7FFF
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
                        status.innerText = `Erreur audio (${error.name}) : ${error.message}`;
                        await logToServer("audio_context_error", {
                            name: error.name,
                            message: error.message,
                            stack: error.stack
                        });
                    }
                };

                ws.onerror = async (event) => {
                    status.innerText = "Connexion WebSocket impossible.";
                    await logToServer("websocket_error", {
                        wsUrl,
                        readyState: ws.readyState,
                        bufferedAmount: ws.bufferedAmount,
                        eventType: event.type
                    });
                    mediaStream.getTracks().forEach(track => track.stop());
                    cancelAnimationFrame(meterAnimationFrame);
                    startBtn.disabled = false;
                };
                ws.onclose = async (event) => {
                    status.innerText = "Déconnecté.";
                    await logToServer("websocket_closed", {
                        wsUrl,
                        code: event.code,
                        reason: event.reason,
                        wasClean: event.wasClean
                    });
                    mediaStream.getTracks().forEach(track => track.stop());
                    cancelAnimationFrame(meterAnimationFrame);
                    startBtn.disabled = false;
                };
            } catch (error) {
                status.innerText = `Microphone indisponible (${error.name}) : ${error.message}`;
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
                startBtn.disabled = false;
            }
        };
    </script>
</body>
</html>
"""

# Serveur HTTP simple pour distribuer la page web au téléphone
class TinyHTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        print(
            f"[HTTP] {self.client_address[0]} GET {self.path} "
            f"user-agent={self.headers.get('User-Agent', 'inconnu')}"
        )
        if self.path == "/microphone/status":
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

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_CONTENT.encode('utf-8'))

    def do_POST(self):
        if self.path == "/microphone":
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
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                print(
                    f"[HTTP] Commande micro invalide depuis "
                    f"{self.client_address[0]}: {error!r}"
                )
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
            print(
                f"[MICRO] Liaison "
                f"{'activée' if payload['enabled'] else 'suspendue'} "
                f"depuis {self.client_address[0]}",
                flush=True
            )
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

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > 65536:
            self.send_error(413, "Diagnostic trop volumineux")
            return

        try:
            diagnostic = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(diagnostic, dict):
                raise ValueError("Le diagnostic doit être un objet JSON")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            print(f"[HTTP] Diagnostic invalide depuis {self.client_address[0]}: {error!r}")
            self.send_error(400, "Diagnostic JSON invalide")
            return

        print(
            f"[NAVIGATEUR] depuis {self.client_address[0]}:\n"
            f"{json.dumps(diagnostic, ensure_ascii=False, indent=2)}",
            flush=True
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[HTTP] {self.client_address[0]} - {format % args}")

def create_tls_context():
    certificate_file = os.environ.get("MICLON_TLS_CERT")
    private_key_file = os.environ.get("MICLON_TLS_KEY")
    if bool(certificate_file) != bool(private_key_file):
        raise RuntimeError(
            "Définissez MICLON_TLS_CERT et MICLON_TLS_KEY ensemble."
        )
    if not certificate_file:
        return None

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        tls_context.load_cert_chain(certificate_file, private_key_file)
    except (OSError, ssl.SSLError) as error:
        raise RuntimeError(
            "Impossible de charger le certificat TLS et sa clé privée."
        ) from error
    return tls_context


def start_http_server(tls_context=None):
    with socketserver.ThreadingTCPServer(
        ("0.0.0.0", 8080), TinyHTTPHandler
    ) as httpd:
        if tls_context is not None:
            httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
            print("[HTTPS] Écoute sur 0.0.0.0:8080", flush=True)
        else:
            print("[HTTP] Écoute sur 0.0.0.0:8080", flush=True)
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
    print("[QR] Scannez ce code avec l'appareil photo du téléphone :")
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
    remote_address = getattr(websocket, "remote_address", "inconnue")
    print(f"[WEBSOCKET] Téléphone connecté depuis {remote_address}.")
    message_count = 0
    byte_count = 0
    try:
        async for message in websocket:
            message_count += 1
            message_size = len(message)
            byte_count += message_size
            if message_count == 1 or message_count % 500 == 0:
                print(
                    f"[AUDIO] depuis {remote_address}: messages={message_count}, "
                    f"dernier_message={message_size} octets, total={byte_count} octets",
                    flush=True
                )
            audio_data = (
                bytes(message)
                if get_microphone_link_enabled()
                else bytes(message_size)
            )
            stream.write(audio_data, exception_on_underflow=False)
    except websockets.exceptions.ConnectionClosed as error:
        print(
            f"[WEBSOCKET] Fermée depuis {remote_address}: "
            f"code={error.code}, raison={error.reason!r}; "
            f"messages={message_count}, octets={byte_count}"
        )
    except Exception as error:
        print(
            f"[WEBSOCKET] Erreur depuis {remote_address} après "
            f"{message_count} messages / {byte_count} octets: {error!r}"
        )
        raise

async def main():
    tls_context = create_tls_context()

    # Trouver l'IP locale du PC pour l'afficher à l'utilisateur
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
    except OSError as error:
        ip = "localhost"
        print(f"[RÉSEAU] Impossible de déterminer l'IP locale: {error!r}")
    finally: s.close()

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
        max_size=None,
        ssl=tls_context,
    ):
        print("=== SERVEUR PRÊT ===")
        print("[WEBSOCKET] Écoute sur 0.0.0.0:5005")
        protocol = "https" if tls_context is not None else "http"
        phone_url = f"{protocol}://{ip}:8080"
        print(f"1. Sur votre téléphone, ouvrez : {phone_url}")
        print_qr_code(phone_url)
        if tls_context is None:
            print(
                "[HTTPS] Non configuré : Chrome Android exige HTTPS pour le "
                "microphone sur une adresse IP du réseau local. Définissez "
                "MICLON_TLS_CERT et MICLON_TLS_KEY avec un certificat contenant "
                "cette adresse IP, puis approuvez son autorité sur le téléphone."
            )
        else:
            print(
                "[HTTPS] Vérifiez que le certificat contient cette adresse IP "
                "et qu'il est approuvé par le téléphone."
            )
        print("2. Cliquez sur 'Démarrer le Micro'")
        print("[CLAVIER] Appuyez sur Échap pour quitter.")

        stop_event = asyncio.Event()
        listener_stop = threading.Event()
        loop = asyncio.get_running_loop()

        def listen_for_escape():
            while not listener_stop.wait(0.05):
                if msvcrt.kbhit() and msvcrt.getwch() == "\x1b":
                    print("\n[CLAVIER] Arrêt demandé.")
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
        print("\nArrêt du serveur.")
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
