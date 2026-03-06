import os
import uuid
import json
import asyncio
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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

async def appeler_groq(system: str, user: str) -> dict:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    }
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(GROQ_URL, headers=headers, json=body)
        data = res.json()
        contenu = data["choices"][0]["message"]["content"]
        contenu = contenu.strip().strip("```json").strip("```").strip()
        return json.loads(contenu)

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

    # 1. Extraire texte PDF
    try:
        doc_pdf = fitz.open(stream=contenu, filetype="pdf")
        texte = ""
        for page in doc_pdf:
            texte += page.get_text()
        doc_pdf.close()
        texte = texte[:6000]
    except Exception:
        texte = "Extraction impossible"

    # 2. Créer ou récupérer étudiant
    etudiant = supabase.table("etudiants").upsert({
        "nom": nom, "email": email, "classe": classe
    }, on_conflict="email").execute()
    etudiant_id = etudiant.data[0]["id"]

    # 3. Upload PDF Supabase Storage
    fichier_nom = f"{uuid.uuid4()}.pdf"
    supabase.storage.from_("rapports-pdf").upload(
        fichier_nom, contenu, {"content-type": "application/pdf"}
    )
    fichier_url = f"{SUPABASE_URL}/storage/v1/object/rapports-pdf/{fichier_nom}"

    # 4. Enregistrer rapport
    rapport = supabase.table("rapports").insert({
        "etudiant_id": etudiant_id,
        "fichier_url": fichier_url,
        "fichier_nom": fichier.filename,
        "statut": "en_cours"
    }).execute()
    rapport_id = rapport.data[0]["id"]

    # 5. Lancer les 4 agents Groq
    try:
        prompt_user = f"Étudiant: {nom}, Classe: {classe}. Texte du rapport: {texte}"

        agent1 = await appeler_groq(
            "Tu es un expert académique. Évalue la structure du rapport de stage (plan, organisation, logique). Réponds UNIQUEMENT en JSON valide: {\"score\": X, \"commentaire\": \"...\"}",
            prompt_user + ". Donne une note sur 20 pour la structure."
        )

        await asyncio.sleep(3)

        agent2 = await appeler_groq(
            "Tu es un expert en linguistique. Évalue la qualité rédactionnelle (clarté, vocabulaire, syntaxe). Réponds UNIQUEMENT en JSON valide: {\"score\": X, \"commentaire\": \"...\"}",
            prompt_user + ". Donne une note sur 20 pour la rédaction."
        )

        await asyncio.sleep(3)

        agent3 = await appeler_groq(
            "Tu es un expert en compétences professionnelles. Évalue les compétences démontrées dans ce rapport. Réponds UNIQUEMENT en JSON valide: {\"score\": X, \"commentaire\": \"...\"}",
            prompt_user + ". Donne une note sur 20 pour les compétences."
        )

        await asyncio.sleep(3)

        agent4 = await appeler_groq(
            "Tu es un conseiller académique. Génère des recommandations personnalisées pour améliorer ce rapport. Réponds UNIQUEMENT en JSON valide: {\"recommandations\": \"...\"}",
            prompt_user + ". Génère des recommandations détaillées."
        )

        note_globale = round((agent1.get("score", 0) + agent2.get("score", 0) + agent3.get("score", 0)) / 3, 2)

        # 6. Sauvegarder résultats
        supabase.table("resultats_ia").insert({
            "rapport_id": rapport_id,
            "note_globale": note_globale,
            "structure_score": agent1.get("score", 0),
            "structure_commentaire": agent1.get("commentaire", ""),
            "redaction_score": agent2.get("score", 0),
            "redaction_commentaire": agent2.get("commentaire", ""),
            "competences_score": agent3.get("score", 0),
            "competences_commentaire": agent3.get("commentaire", ""),
            "recommandations": agent4.get("recommandations", "")
        }).execute()

        # 7. Mettre à jour statut
        supabase.table("rapports").update({"statut": "analyse_terminee"}).eq("id", rapport_id).execute()

    except Exception as e:
        supabase.table("rapports").update({"statut": "en_attente"}).eq("id", rapport_id).execute()

    return {
        "status": "success",
        "message": f"Rapport de {nom} analysé avec succès",
        "rapport_id": rapport_id
    }

@app.get("/resultats/{email}")
def get_resultats(email: str):
    etudiant = supabase.table("etudiants").select("*").eq("email", email).execute()
    if not etudiant.data:
        raise HTTPException(status_code=404, detail="Étudiant non trouvé")
    etudiant_id = etudiant.data[0]["id"]
    rapports = supabase.table("rapports").select("*, resultats_ia(*)").eq("etudiant_id", etudiant_id).execute()
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
