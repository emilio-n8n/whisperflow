import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Le faux serveur Groq est en local : ne jamais passer par un proxy système.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
