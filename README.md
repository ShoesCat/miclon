# Miclon

Miclon turns a phone's microphone into a wireless audio input for a Windows PC.
The phone opens a web page served by the Python program and streams microphone
audio to the PC over a WebSocket connection. The PC routes the audio to a
VB-AUDIO Virtual Cable output.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- A VB-AUDIO Virtual Cable installation, with its output device available
- A phone and PC on the same trusted local network
- A modern phone browser that supports microphone access

Install the Python packages from PowerShell in the project directory:

```powershell
py -m pip install -r requirements.txt
```

Run the program:

```powershell
py v3.py
```

The program prints a local URL and a QR code. Open the URL on the phone, grant
microphone permission, and select **Start Microphone**. Press Escape in the
program's console to stop it.

The program listens on TCP ports **8080** (web page and control requests) and
**5005** (audio WebSocket). It searches for a VB-AUDIO Virtual Cable output
device when it starts and reports an error if none is available.

### HTTPS on a phone

Phone browsers generally require a secure context (HTTPS) to grant microphone
access to a page opened using a local network IP address. Without a TLS
certificate trusted by the phone, the page may load but microphone access may
not work.

To configure TLS, set `MICLON_TLS_CERT` and `MICLON_TLS_KEY` to the paths of a
certificate and its matching private key before running the program. The
certificate must be valid for the PC's local IP address and trusted by the
phone. **Never publish a private key, certificate authority key, or other
private certificate material.**

## Network and privacy notice

This is a local-network utility, not an internet-facing service. The HTTP and
WebSocket servers bind to all network interfaces and do not authenticate
clients. Anyone who can reach the ports may access the page and control the
microphone link; TLS encrypts traffic but does not add authentication. Use it
only on a trusted network, and do not expose or forward ports 8080 or 5005 to
the internet.

Audio is streamed from the phone to the PC and written to the selected virtual
audio output. This program does not intentionally save audio recordings.
Diagnostic messages are printed in the PC console and can include the
connecting device's IP address, browser details, and error information.

## License

This project is distributed under the MIT License. See [LICENSE](LICENSE).

---

# Miclon (français)

Miclon transforme le microphone d'un téléphone en entrée audio sans fil pour
un PC Windows. Le téléphone ouvre une page web fournie par le programme Python
et transmet le son au PC par WebSocket. Le PC envoie l'audio vers une sortie
VB-AUDIO Virtual Cable.

## Prérequis

- Windows 10 ou 11
- Python 3.10 ou version ultérieure
- VB-AUDIO Virtual Cable installé et sa sortie audio disponible
- Un téléphone et un PC connectés au même réseau local de confiance
- Un navigateur mobile récent prenant en charge l'accès au microphone

Dans PowerShell, depuis le dossier du projet, installe les dépendances :

```powershell
py -m pip install -r requirements.txt
```

Lance le programme :

```powershell
py v3.py
```

Le programme affiche une adresse locale et un code QR. Ouvre l'adresse sur le
téléphone, autorise l'accès au microphone et appuie sur **Démarrer le Micro**.
Appuie sur Échap dans la console du programme pour l'arrêter.

Le programme écoute sur les ports TCP **8080** (page web et commandes) et
**5005** (WebSocket audio). Au démarrage, il recherche une sortie VB-AUDIO
Virtual Cable et affiche une erreur si elle est introuvable.

### HTTPS sur le téléphone

Les navigateurs mobiles exigent généralement un contexte sécurisé (HTTPS) pour
autoriser le microphone lorsque la page est ouverte avec l'adresse IP du réseau
local. Sans certificat TLS approuvé par le téléphone, la page peut s'afficher
sans que l'accès au microphone fonctionne.

Pour configurer TLS, définis `MICLON_TLS_CERT` et `MICLON_TLS_KEY` avec les
chemins du certificat et de sa clé privée correspondante avant de lancer le
programme. Le certificat doit être valide pour l'adresse IP locale du PC et
approuvé par le téléphone. **Ne publie jamais une clé privée, une clé
d'autorité de certification ou d'autres éléments privés de certificat.**

## Réseau et confidentialité

Cet outil est destiné à un réseau local, pas à être exposé sur Internet. Les
serveurs HTTP et WebSocket écoutent sur toutes les interfaces réseau et
n'authentifient pas les clients. Toute personne pouvant joindre les ports peut
accéder à la page et contrôler la liaison du microphone ; TLS chiffre les
communications, mais n'ajoute pas d'authentification. Utilise l'outil
uniquement sur un réseau de confiance et n'expose ni ne redirige les ports
8080 et 5005 vers Internet.

L'audio est transmis du téléphone au PC puis écrit sur la sortie audio
virtuelle choisie. Le programme n'enregistre pas intentionnellement l'audio.
Les messages de diagnostic sont affichés dans la console du PC et peuvent
contenir l'adresse IP du client, des informations sur le navigateur et des
informations d'erreur.

## Licence

Ce projet est distribué sous licence MIT. Consulte [LICENSE](LICENSE).
