import os
import uuid
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import httpx
import fitz

app = FastAPI(title="ENA Intelligence Rapport API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

@app.get("/")
def home():
    return {"message": "ENA Intelligence Rapport API", "status": "ok"}

@app.post("/upload")
async def upload_rapport(
    nom: str = Form(...),
    email: str = Form(...),
    classe: str = Form(...),
    fichier: UploadFile = File(...)
):
    if not fichier.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Fichier PDF uniquement")

    contenu = await fichier.read()

    # 1. Extraire le texte du PDF
    try:
        doc_pdf = fitz.open(stream=contenu, filetype="pdf")
        texte = ""
        for page in doc_pdf:
            texte += page.get_text()
        doc_pdf.close()
        texte = texte[:8000]
    except Exception:
        texte = "Extraction impossible"

    # 2. Créer ou récupérer l'étudiant
    etudiant = supabase.table("etudiants").upsert({
        "nom": nom, "email": email, "classe": classe
    }, on_conflict="email").execute()
    etudiant_id = etudiant.data[0]["id"]

    # 3. Upload PDF dans Supabase Storage
    fichier_nom = f"{uuid.uuid4()}.pdf"
    supabase.storage.from_("rapports-pdf").upload(
        fichier_nom, contenu, {"content-type": "application/pdf"}
    )
    fichier_url = f"{SUPABASE_URL}/storage/v1/object/rapports-pdf/{fichier_nom}"

    # 4. Enregistrer le rapport
    rapport = supabase.table("rapports").insert({
        "etudiant_id": etudiant_id,
        "fichier_url": fichier_url,
        "fichier_nom": fichier.filename,
        "statut": "en_attente"
    }).execute()
    rapport_id = rapport.data[0]["id"]

    # 5. Déclencher n8n avec le texte extrait
    if N8N_WEBHOOK_URL:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(N8N_WEBHOOK_URL, json={
                "rapport_id": rapport_id,
                "etudiant_id": etudiant_id,
                "nom": nom,
                "email": email,
                "classe": classe,
                "fichier_url": fichier_url,
                "texte_rapport": texte
            })

    return {
        "status": "success",
        "message": f"Rapport de {nom} reçu et en cours d'analyse",
        "rapport_id": rapport_id
    }

@app.get("/resultats/{email}")
def get_resultats(email: str):
    etudiant = supabase.table("etudiants")\
        .select("*").eq("email", email).execute()
    if not etudiant.data:
        raise HTTPException(status_code=404, detail="Étudiant non trouvé")
    etudiant_id = etudiant.data[0]["id"]
    rapports = supabase.table("rapports")\
        .select("*, resultats_ia(*)")\
        .eq("etudiant_id", etudiant_id).execute()
    return {"etudiant": etudiant.data[0], "rapports": rapports.data}

@app.get("/admin/stats")
def get_stats():
    etudiants = supabase.table("etudiants").select("*", count="exact").execute()
    rapports = supabase.table("rapports").select("*", count="exact").execute()
    analyses = supabase.table("resultats_ia").select("note_globale").execute()
    notes = [r["note_globale"] for r in analyses.data if r["note_globale"]]
    moyenne = sum(notes) / len(notes) if notes else 0
    return {
        "total_etudiants": etudiants.count,
        "total_rapports": rapports.count,
        "moyenne_generale": round(moyenne, 2),
        "analyses_terminees": len(notes)
    }
PyMuPDF
