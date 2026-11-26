# Banca Inteligente

Prototipo escolar del Reto Banamex. El portal FastAPI usa Google ADK y corre en Windows. Docker Compose inicia PostgreSQL 17 con pgvector y carga datos sint?ticos de enero a junio de 2026 en un volumen nuevo.

## Arranque en Windows

Requiere Python 3.12, Docker Desktop en modo Linux y una clave de Gemini. Desde el clon, con Docker Desktop abierto:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preparar_postgres.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\postgres.ps1 up
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\postgres.ps1 verify

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Introduce tu clave en el archivo local .env. El generador crea .env.postgres con claves distintas para el administrador y el rol de lectura. Si Docker ya ejecuta otra instalaci?n en la misma m?quina, usa un proyecto Compose y un puerto de PostgreSQL diferentes.

Crea el usuario web local y arranca el portal. El script solicita la contrase?a sin mostrarla.

```powershell
$env:BANCO_USERS_FILE = Join-Path $PWD 'config\auth_users.local.json'
.\.venv\Scripts\python.exe scripts\provision_web_user.py empleado_demo Empleado
.\.venv\Scripts\python.exe -m banco_agentes.web --provider gemini --port 8004
```

Abre <http://127.0.0.1:8004/> e inicia sesi?n con el usuario que acabas de crear.

## Material local

El volumen de PostgreSQL, las credenciales, los adjuntos de usuarios y el corpus documental se conservan en la m?quina donde se usan. Una base reci?n inicializada genera importes sint?ticos nuevos. Las funciones y pruebas documentales que consultan el corpus requieren restaurarlo localmente.

El SQL de Cliente est? en docker/postgres/client_access.sql. Para habilitar el perfil Cliente, aplica ese SQL con el administrador local, establece la contrase?a de banco_cliente y sumin?strala al proceso mediante BANCO_CLIENT_DB_PASSWORD. Usa scripts/provision_web_user.py con el rol Cliente y el n?mero de cliente sint?tico correspondiente.

Los archivos de fixtures/documents son ejemplos ficticios para las pruebas.
