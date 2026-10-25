# Miclon V6

Miclon V6 transforme le microphone d'un téléphone en entrée audio sans fil
pour un PC Windows. Télécharge [Miclon.exe](./dist/Miclon.exe) et lance-le : Python
n'a pas besoin d'être installé.

## Prérequis

- Windows 10 ou 11 (64 bits)
- [VB-AUDIO Virtual Cable](https://vb-audio.com/Cable/) installé pour la
  sortie audio virtuelle
- Un téléphone et un PC connectés au même réseau local de confiance
- Un navigateur mobile prenant en charge l'accès au microphone

## Utilisation

1. Lance `dist\Miclon.exe` sur le PC. Autorise l'application dans le pare-feu Windows
   si nécessaire.
2. Saisis dans le navigateur du téléphone l'adresse HTTPS et le code PIN
   affichés dans la console du PC, ou scanne le QR code.
3. Accepte l'avertissement du certificat local si le navigateur le présente,
   autorise l'accès au microphone, puis démarre le micro.
4. Appuie sur Échap dans la console du PC pour arrêter V6.

Le téléphone et le PC doivent rester connectés au même réseau. Les réglages
de V6 permettent notamment de changer la langue, d'activer le filtre
antiparasite et d'écouter aussi l'audio sur la sortie par défaut du PC.

V6 écoute sur les ports TCP **8080** (page et commandes) et **5005** (audio).
Il s'agit d'un outil pour réseau local : ne redirige pas ces ports vers
Internet. L'audio n'est pas intentionnellement enregistré. Le flux RSS
partenaire par défaut nécessite une connexion Internet ; le fonctionnement
audio local reste destiné au réseau local.

## Recompiler depuis les sources

Le code source et les fichiers nécessaires au build sont disponibles dans ce
dépôt. Sur Windows 64 bits, avec Python installé et `py` disponible dans le
terminal, exécute :

```bat
py -m pip install -r requirements-build.txt
build_v6.bat
```

L'exécutable reconstruit est créé dans `dist\Miclon.exe`. Le fichier
`V6.spec` inclut les pages et ressources utilisées par l'application.

## License

This project is distributed under the MIT License. See [LICENSE](./LICENSE).

---

# Miclon V6 (English)

Miclon V6 turns a phone's microphone into wireless audio input for a Windows
PC. Download [Miclon.exe](./dist/Miclon.exe) and run it; Python does not need to be
installed.

## Requirements

- 64-bit Windows 10 or 11
- [VB-AUDIO Virtual Cable](https://vb-audio.com/Cable/) installed for virtual
  audio output
- A phone and PC on the same trusted local network
- A mobile browser that supports microphone access

## Use

1. Run `dist\Miclon.exe` on the PC. Allow it through Windows Firewall if prompted.
2. Enter the HTTPS address and PIN shown in the PC console in the phone's
   browser, or scan the QR code.
3. Accept the local certificate warning if shown, allow microphone access,
   and start the microphone.
4. Press Escape in the PC console to stop V6.

V6 listens on TCP ports **8080** (page and controls) and **5005** (audio).
This is a local-network utility; do not expose or forward these ports to the
internet. Audio is not intentionally recorded. The default partner RSS feed
requires an internet connection; local audio operation is intended for the
local network.

## Rebuild from source

The source code and build files are available in this repository. On 64-bit
Windows, with Python installed and the `py` launcher available in a terminal,
run:

```bat
py -m pip install -r requirements-build.txt
build_v6.bat
```

The rebuilt executable is created at `dist\Miclon.exe`. `V6.spec` includes the
pages and resources used by the application.

## License

This project is distributed under the MIT License. See [LICENSE](./LICENSE).
