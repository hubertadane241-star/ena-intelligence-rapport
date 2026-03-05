from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "ENA AI Reports API running"}
