# OpenDesk — Pubblicazione e installazione

## 📦 Pubblicazione su PyPI (per maintainer)

### Prerequisiti

- Un account su [PyPI](https://pypi.org/) (per la pubblicazione ufficiale)
- Opzionale: account su [TestPyPI](https://test.pypi.org/) per test

### Build del pacchetto

```bash
# Installa gli strumenti di build
pip install build twine

# Genera wheel + sdist
python -m build

# Output in dist/
ls dist/
# opendesk-1.0.0-py3-none-any.whl
# opendesk-1.0.0.tar.gz
```

> Nota: con `uv`: `uv run python -m build`

### Pubblicazione manuale

```bash
# Carica su PyPI
twine upload dist/*

# Per testare prima su TestPyPI
twine upload --repository testpypi dist/*
```

### Pubblicazione automatica (GitHub Actions)

1. Crea un tag `v*` sul repository:
   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```
2. La CI su GitHub:
   - Builda wheel + sdist
   - Pubblica su PyPI (con Trusted Publishing)
   - Crea una GitHub Release con gli artifact

### Trusted Publishing (PyPI)

Il workflow CI usa `pypa/gh-action-pypi-publish` con `id-token: write`.
Per abilitarlo:

1. Vai su https://pypi.org/manage/account/publishing/
2. Aggiungi un trusted publisher per `opendesk`
3. Imposta:
   - **PyPI Project:** `opendesk`
   - **GitHub Owner:** `opendesk`
   - **GitHub Repository:** `opendesk-client`
   - **Workflow name:** `ci.yml`
   - **Environment name:** `pypi`

## 💻 Installazione (per utenti finali)

### Bootstrap (consigliato)

```bash
# Linux / macOS
curl -fsSL https://opendesk.io/bootstrap.sh | bash
```

```powershell
# Windows (PowerShell come Amministratore)
iwr -useb https://opendesk.io/bootstrap.ps1 | iex
```

Il bootstrap script:
1. Installa Python 3.12+ se mancante
2. Installa le dipendenze di sistema (ffmpeg, libxtst, pipewire, ecc.)
3. Esegue `pip install opendesk` (o `pipx install opendesk`)
4. Crea il menu entry / shortcut

### Via pip

```bash
pip install opendesk
```

**Disinstallare:**

```bash
pip uninstall opendesk
```

### Via pipx (gestione isolata)

```bash
pipx install opendesk
pipx upgrade opendesk   # Per aggiornare
pipx uninstall opendesk # Per disinstallare
```

### Da sorgente (sviluppo)

```bash
git clone https://github.com/opendesk/opendesk-client
cd opendesk-client

# Con uv (consigliato)
uv sync
uv run opendesk

# Con pip
pip install -e ".[dev]"
opendesk
```

### Dipendenze di sistema

Verifica le dipendenze di sistema con:

```bash
opendesk --install-system-deps
```

### Pacchetti standalone (legacy)

Per chi preferisce un eseguibile standalone (senza Python), sono disponibili
build PyInstaller nei **GitHub Releases**:
https://github.com/opendesk/opendesk-client/releases

```bash
# Linux (AppImage)
chmod +x opendesk-*.AppImage
./opendesk-*.AppImage

# Windows (estrarre lo zip ed eseguire)
opendesk.exe

# macOS (.dmg)
# Montare e trascinare OpenDesk.app in /Applications
```

## 📁 Struttura file

```
scripts/
├── bootstrap.sh         ← Bootstrap installer Linux/macOS (NUOVO)
├── bootstrap.ps1        ← Bootstrap installer Windows (NUOVO)
├── install.sh           ← Legacy (reindirizza a bootstrap.sh)
├── install.ps1          ← Legacy (reindirizza a bootstrap.ps1)
└── legacy/              ← Script del vecchio sistema di distribuzione
    ├── opendesk.spec    ← Specifica PyInstaller
    ├── build.sh         ← Vecchio orchestratore di build
    ├── package-linux.sh
    ├── package-windows.sh
    ├── package-macos.sh
    ├── upload.sh
    ├── opendesk.nsi
    └── opendesk.desktop
```
