import copy
import uuid
import os
from starlette.requests import Request
from unidecode import unidecode
from sqlalchemy.orm import Session
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import urllib.parse
from llama_index.core.postprocessor import SimilarityPostprocessor
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.schema import Document
from llama_index.core.retrievers import VectorIndexRetriever
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
import traceback
from tempfile import NamedTemporaryFile
import re
from pathlib import Path
import jwt
import logging
import json
import base64
from datetime import timedelta
import secrets
from fastapi.responses import RedirectResponse
from app import config
import sentry_sdk

print("""
  ___ ___ ___ _____ _   ___      _.--'"'.
 | _ \ __/ __|_   _/_\ |_ _|    (  ( (   )
 |   / _|\__ \ | |/ _ \ | |     (o)_    ) )
 |_|_\___|___/ |_/_/ \_\___|        (o)_.'
                            
""")

from app.helper import chat_main, question_main
from app.vectordb import tools
from app.project import Project
from modules.loaders import LOADERS
from modules.embeddings import EMBEDDINGS
from app.models.models import ClassifierModel, ClassifierResponse, FindModel, IngestResponse, LLMModel, LLMUpdate, ProjectModel, ProjectModelUpdate, ProjectsResponse, QuestionModel, ChatModel, TextIngestModel, Tool, URLIngestModel, User, UserCreate, UserUpdate, UsersResponse
from app.loaders.url import SeleniumWebReader
from app.database import dbc, get_db
from app.brain import Brain
from app.auth import create_access_token, get_current_username, get_current_username_admin, get_current_username_project, get_current_username_user
from app.tools import get_logger
from app.vectordb.tools import FindFileLoader, IndexDocuments, ExtractKeywordsForMetadata


logging.basicConfig(level=config.LOG_LEVEL)
logging.getLogger('passlib').setLevel(logging.ERROR)

if config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        enable_tracing=True
    )


app = FastAPI(
    title="RestAI",
    description="RestAI is an AIaaS (AI as a Service) open-source platform. Built on top of Llamaindex, Langchain and Transformers. Supports any public LLM supported by LlamaIndex and any local LLM suported by Ollama. Precise embeddings usage and tuning.",
    version="4.0.0",
    contact={
        "name": "Pedro Dias",
        "url": "https://github.com/apocas/restai",
        "email": "petermdias@gmail.com",
    },
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
)

if config.RESTAI_DEV:
    print("Running in development mode!")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

brain = Brain()
logs_inference = get_logger("inference")


@app.get("/")
async def get(request: Request):
    return "RESTAI, so many 'A's and 'I's, so little time..."


@app.get("/info")
async def get_info(user: User = Depends(get_current_username), db: Session = Depends(get_db)):
    output = {
        "version": app.version,
        "loaders": list(LOADERS.keys()),
        "embeddings": [],
        "llms": []
    }

    llms = dbc.get_llms(db)
    for llm in llms:
        output["llms"].append({
            "name": llm.name,
            "privacy": llm.privacy,
            "description": llm.description,
            "type": llm.type
        })

    for embedding in EMBEDDINGS:
        _, _, privacy, description, _ = EMBEDDINGS[embedding]
        output["embeddings"].append({
            "name": embedding,
            "privacy": privacy,
            "description": description
        })
    return output


@app.get("/sso")
async def get_sso(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)

    if "jwt" not in params:
        raise HTTPException(
            status_code=400, detail="Missing JWT token")

    try:
        data = jwt.decode(params["jwt"], config.RESTAI_SSO_SECRET, algorithms=[config.RESTAI_SSO_ALG])
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail="Invalid token"
        )

    user = dbc.get_user_by_username(db, data["preferred_username"])
    if user is None:
        user = dbc.create_user(db,
                               data["preferred_username"], None,
                               False,
                               False)
        user.sso = config.RESTAI_SSO_CALLBACK
        db.commit()

    new_token = create_access_token(
        data={"username": user.username}, expires_delta=timedelta(minutes=1440))

    response = RedirectResponse("./admin")
    response.set_cookie(key="restai_token", value=new_token, samesite="strict", expires=86400)

    return response


@app.get("/users/{username}/sso")
async def get_user(username: str, db: Session = Depends(get_db)):
    try:
        user = dbc.get_user_by_username(db, username)
        if user is None:
            return {"sso": config.RESTAI_SSO_CALLBACK}
        return {"sso": user.sso}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=404, detail=str(e))


@app.get("/users/{username}", response_model=User)
async def get_user(username: str, user: User = Depends(get_current_username_user), db: Session = Depends(get_db)):
    try:
        user_model = User.model_validate(
            dbc.get_user_by_username(db, username))
        user_model_copy = copy.deepcopy(user_model)
        del user_model_copy.api_key
        return user_model
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=404, detail=str(e))


@app.post("/users/{username}/apikey")
async def get_user(username: str, user: User = Depends(get_current_username_user), db: Session = Depends(get_db)):
    try:
        useru = dbc.get_user_by_username(db, username)
        if useru is None:
            raise Exception("User not found")

        apikey = uuid.uuid4().hex + secrets.token_urlsafe(32)
        dbc.update_user(db, useru, UserUpdate(api_key=apikey))
        return {"api_key": apikey}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=404, detail=str(e))


@app.get("/users", response_model=UsersResponse)
async def get_users(
        user: User = Depends(get_current_username_admin),
        db: Session = Depends(get_db)):
    users = dbc.get_users(db)
    users_final = []

    for user_model in users:
        user_model_copy = copy.deepcopy(User.model_validate(user_model))
        del user_model_copy.api_key
        users_final.append(user_model_copy)

    return {"users": users_final}


@app.get("/llms/{llmname}", response_model=LLMModel)
async def get_llm(llmname: str, user: User = Depends(get_current_username), db: Session = Depends(get_db)):
    try:
        return LLMModel.model_validate(dbc.get_llm_by_name(db, llmname))
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=404, detail=str(e))


@app.get("/llms", response_model=list[LLMModel])
async def get_llms(
        user: User = Depends(get_current_username),
        db: Session = Depends(get_db)):
    users = dbc.get_llms(db)
    return users


@app.post("/llms")
async def create_llm(llmc: LLMModel,
                      user: User = Depends(get_current_username_admin),
                      db: Session = Depends(get_db)):
    try:
        llm = dbc.create_llm(db, llmc.name, llmc.class_name, llmc.options, llmc.privacy, llmc.description, llmc.type)
        return llm
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500,
            detail='Failed to create LLM ' + llmc.name)


@app.patch("/llms/{llmname}")
async def edit_project(llmname: str, llmUpdate: LLMUpdate, user: User = Depends(get_current_username_admin), db: Session = Depends(get_db)):
    try:
        llm = dbc.get_llm_by_name(db, llmname)
        if llm is None:
            raise Exception("LLM not found")
        if dbc.update_llm(db, llm, llmUpdate):
            brain.loadLLM(llmname, db)
            return {"project": llmname}
        else:
            raise HTTPException(
                status_code=404, detail='LLM not found')
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.delete("/llms/{llmname}")
async def delete_llm(llmname: str,
                      user: User = Depends(get_current_username_admin),
                      db: Session = Depends(get_db)):
    try:
        llm = dbc.get_llm_by_name(db, llmname)
        if llm is None:
            raise Exception("LLM not found")
        dbc.delete_llm(db, llm)
        return {"deleted": llmname}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.post("/users", response_model=User)
async def create_user(userc: UserCreate,
                      user: User = Depends(get_current_username_admin),
                      db: Session = Depends(get_db)):
    try:
        userc.username = unidecode(
            userc.username.strip().lower().replace(" ", "."))
        userc.username = re.sub(r'[^\w\-.]+', '', userc.username)

        user = dbc.create_user(db,
                               userc.username,
                               userc.password,
                               userc.is_admin,
                               userc.is_private)
        user_model_copy = copy.deepcopy(user)
        user_model_copy.api_key = None
        user_model_copy.id = None
        user_model_copy.projects = None
        return user_model_copy
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500,
            detail='Failed to create user ' + userc.username)


@app.patch("/users/{username}", response_model=User)
async def update_user(
        username: str,
        userc: UserUpdate,
        user: User = Depends(get_current_username_user),
        db: Session = Depends(get_db)):
    try:
        useru = dbc.get_user_by_username(db, username)
        if useru is None:
            raise Exception("User not found")

        if not user.is_admin and userc.is_admin is True:
            raise Exception("Insuficient permissions")

        dbc.update_user(db, useru, userc)

        if userc.projects is not None:
            useru.projects = []
            
            for project in userc.projects:
                projectdb = dbc.get_project_by_name(db, project)
                
                if projectdb is None:
                    raise Exception("Project not found")
                useru.projects.append(projectdb)
            db.commit()
        return useru
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.delete("/users/{username}")
async def delete_user(username: str,
                      user: User = Depends(get_current_username_admin),
                      db: Session = Depends(get_db)):
    try:
        userl = dbc.get_user_by_username(db, username)
        if userl is None:
            raise Exception("User not found")
        dbc.delete_user(db, userl)
        return {"deleted": username}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.get("/projects", response_model=ProjectsResponse)
async def get_projects(request: Request, user: User = Depends(get_current_username), db: Session = Depends(get_db)):
    if user.is_admin:
        projects = dbc.get_projects(db)
    else:
        projects = []
        for project in user.projects:
            for p in dbc.get_projects(db):
                if project.name == p.name:
                    projects.append(p)

    for project in projects:
        try:
            model = brain.getLLM(project.llm, db)
            project.llm_type = model.props.type
            project.llm_privacy = model.props.privacy
        except Exception as e:
            project.llm_type = "unknown"
            project.llm_privacy = "unknown"

    return {"projects": projects}


@app.get("/projects/{projectName}")
async def get_project(projectName: str, user: User = Depends(get_current_username_project), db: Session = Depends(get_db)):
    try:
        project = brain.findProject(projectName, db)
        
        if project is None:
            raise HTTPException(
                status_code=404, detail='Project not found')

        output = project.model.model_dump()
        final_output = {}
        
        try:
            llm_model = brain.getLLM(project.model.llm, db)
        except Exception as e:
            llm_model = None

        final_output["name"] = output["name"]
        final_output["type"] = output["type"]
        final_output["llm"] = output["llm"]
        final_output["human_name"] = output["human_name"]
        final_output["human_description"] = output["human_description"]
        final_output["censorship"] = output["censorship"]
        final_output["guard"] = output["guard"]
        
        if project.model.type == "rag":
            if project.vector is not None:
                chunks = project.vector.info()
                if chunks is not None:
                    final_output["chunks"] = chunks
            else:
                final_output["chunks"] = 0
            final_output["embeddings"] = output["embeddings"]
            final_output["k"] = output["k"]
            final_output["score"] = output["score"]
            final_output["vectorstore"] = output["vectorstore"]
            final_output["system"] = output["system"]
            final_output["llm_rerank"] = output["llm_rerank"]
            final_output["colbert_rerank"] = output["colbert_rerank"]
            final_output["cache"] = output["cache"]
            final_output["cache_threshold"] = output["cache_threshold"]
        
        if project.model.type == "inference":
            final_output["system"] = output["system"]
            
        if project.model.type == "agent":
            final_output["system"] = output["system"]
            final_output["tools"] = output["tools"]
            
        if project.model.type == "ragsql":
            final_output["system"] = output["system"]
            final_output["tables"] = output["tables"]
            if output["connection"] is not None:
                final_output["connection"] = re.sub(
                r'(?<=://).+?(?=@)', "xxxx:xxxx", output["connection"])
            
        if project.model.type == "router":
            final_output["entrances"] = output["entrances"]
            
        if llm_model:
            final_output["llm_type"]=llm_model.props.type
            final_output["llm_privacy"]=llm_model.props.privacy

        return final_output
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=404, detail=str(e))


@app.delete("/projects/{projectName}")
async def delete_project(projectName: str, user: User = Depends(get_current_username_project), db: Session = Depends(get_db)):
    try:
        proj = brain.findProject(projectName, db)
        
        if proj is not None:
            dbc.delete_project(db, dbc.get_project_by_name(db, projectName))
            proj.delete()
        else:
            raise HTTPException(
                status_code=404, detail='Project not found')
            
        return {"project": projectName}

    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.patch("/projects/{projectName}")
async def edit_project(projectName: str, projectModelUpdate: ProjectModelUpdate, user: User = Depends(get_current_username_project), db: Session = Depends(get_db)):
  
    if projectModelUpdate.llm and brain.getLLM(projectModelUpdate.llm, db) is None:
        raise HTTPException(
            status_code=404,
            detail='LLM not found')

    if user.is_private:
        llm_model = brain.getLLM(projectModelUpdate.llm, db)
        if llm_model.props.privacy != "private":
            raise HTTPException(
                status_code=403,
                detail='User not allowed to use public models')

    try:
        if dbc.editProject(projectName, projectModelUpdate, db):
            return {"project": projectName}
        else:
            raise HTTPException(
                status_code=404, detail='Project not found')
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.post("/projects")
async def create_project(projectModel: ProjectModel, user: User = Depends(get_current_username), db: Session = Depends(get_db)):
    projectModel.human_name = projectModel.name.strip()
    projectModel.name = unidecode(
        projectModel.name.strip().lower().replace(" ", "_"))
    projectModel.name = re.sub(r'[^\w\-.]+', '', projectModel.name)
    
    if projectModel.type not in ["rag", "inference", "router", "ragsql", "vision", "agent"]:
        raise HTTPException(
            status_code=404,
            detail='Invalid project type')

    if projectModel.name.strip() == "":
        raise HTTPException(
            status_code=400,
            detail='Invalid project name')
        
    if config.RESTAI_DEMO:
        if projectModel.type == "ragsql":
            raise HTTPException(
                status_code=403,
                detail='Demo mode, not allowed to create RAGSQL projects')

    if projectModel.type == "rag" and projectModel.embeddings not in EMBEDDINGS:
        raise HTTPException(
            status_code=404,
            detail='Embeddings not found')
    if brain.getLLM(projectModel.llm, db) is None:
        raise HTTPException(
            status_code=404,
            detail='LLM not found')

    proj = brain.findProject(projectModel.name, db)
    if proj is not None:
        raise HTTPException(
            status_code=403,
            detail='Project already exists')

    if user.is_private:
        llm_model = brain.getLLM(projectModel.llm, db)
        if llm_model.props.privacy != "private":
            raise HTTPException(
                status_code=403,
                detail='User allowed to private models only')

        if projectModel.type == "rag":
            _, _, embedding_privacy, _, _ = EMBEDDINGS[
                projectModel.embeddings]
            if embedding_privacy != "private":
                raise HTTPException(
                    status_code=403,
                    detail='User allowed to private models only')

    try:
        dbc.create_project(
            db,
            projectModel.name,
            projectModel.embeddings,
            projectModel.llm,
            projectModel.vectorstore,
            projectModel.human_name,
            projectModel.type,
        )
        project = Project(projectModel)
        
        if(project.model.vectorstore):
            project.vector = tools.findVectorDB(project)(brain, project)
        
        projectdb = dbc.get_project_by_name(db, project.model.name)
        
        userdb = dbc.get_user_by_id(db, user.id)
        userdb.projects.append(projectdb)
        db.commit()
        return {"project": projectModel.name}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.post("/projects/{projectName}/embeddings/reset")
async def reset_embeddings(
        projectName: str,
        user: User = Depends(get_current_username_project),
        db: Session = Depends(get_db)):
    try:
        project = brain.findProject(projectName, db)

        if project.model.type != "rag":
            raise HTTPException(
                status_code=400, detail='{"error": "Only available for RAG projects."}')

        project.vector.reset(brain)

        return {"project": project.model.name}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=404, detail=str(e))

@app.post("/projects/{projectName}/clone/{newProjectName}")
async def clone_project(projectName: str, newProjectName: str,
                         user: User = Depends(get_current_username_project),
                         db: Session = Depends(get_db)):
    project = brain.findProject(projectName, db)
    if project is None:
        raise HTTPException(
            status_code=404, detail='Project not found')
        
    newProject = dbc.get_project_by_name(db, newProjectName)
    if newProject is not None:
        raise HTTPException(
            status_code=403, detail='Project already exists')
        
    project_db = dbc.get_project_by_name(db, projectName)
    
    newProject_db = dbc.create_project(
        db,
        newProjectName,
        project.model.embeddings,
        project.model.llm,
        project.model.vectorstore,
        project.model.type
    )
    
    newProject_db.system = project.model.system
    newProject_db.censorship = project.model.censorship
    newProject_db.k = project.model.k
    newProject_db.score = project.model.score
    newProject_db.llm_rerank = project.model.llm_rerank
    newProject_db.colbert_rerank = project.model.colbert_rerank
    newProject_db.cache = project.model.cache
    newProject_db.cache_threshold = project.model.cache_threshold
    newProject_db.guard = project.model.guard
    newProject_db.human_name = project.model.human_name
    newProject_db.human_description = project.model.human_description
    newProject_db.tables = project.model.tables
    newProject_db.connection = project.model.connection
    
    for user in project_db.users:
        newProject_db.users.append(user)
        
    for entrance in project_db.entrances:
        newProject_db.entrances.append(entrance)
        
    db.commit()

    return {"project": newProjectName}

@app.post("/projects/{projectName}/embeddings/search")
async def find_embedding(projectName: str, embedding: FindModel,
                         user: User = Depends(get_current_username_project),
                         db: Session = Depends(get_db)):
    project = brain.findProject(projectName, db)

    if project.model.type != "rag":
        raise HTTPException(
            status_code=400, detail='{"error": "Only available for RAG projects."}')

    output = []

    if (embedding.text):
        k = embedding.k or project.model.k or 2

        if (embedding.score != None):
            threshold = embedding.score
        else:
            threshold = embedding.score or project.model.score or 0.2

        retriever = VectorIndexRetriever(
            index=project.vector.index,
            similarity_top_k=k,
        )

        query_engine = RetrieverQueryEngine.from_args(
            retriever=retriever,
            node_postprocessors=[SimilarityPostprocessor(
                similarity_cutoff=threshold)],
            response_mode="no_text"
        )

        response = query_engine.query(embedding.text)

        for node in response.source_nodes:
            output.append(
                {"source": node.metadata["source"], "score": node.score, "id": node.node_id})

    elif (embedding.source):
        output = project.vector.list_source(embedding.source)

    return {"embeddings": output}


@app.get("/projects/{projectName}/embeddings/source/{source}")
async def get_embedding(projectName: str, source: str,
                        user: User = Depends(get_current_username_project),
                        db: Session = Depends(get_db)):
    project = brain.findProject(projectName, db)

    if project.model.type != "rag":
        raise HTTPException(
            status_code=400, detail='{"error": "Only available for RAG projects."}')

    docs = project.vector.find_source(base64.b64decode(source).decode('utf-8'))

    if (len(docs['ids']) == 0):
        return {"ids": []}
    else:
        return docs


@app.get("/projects/{projectName}/embeddings/id/{id}")
async def get_embedding(projectName: str, id: str,
                        user: User = Depends(get_current_username_project),
                        db: Session = Depends(get_db)):
    project = brain.findProject(projectName, db)

    if project.model.type != "rag":
        raise HTTPException(
            status_code=400, detail='{"error": "Only available for RAG projects."}')

    chunk = project.vector.find_id(id)
    return chunk


@app.post("/projects/{projectName}/embeddings/ingest/text", response_model=IngestResponse)
async def ingest_text(projectName: str, ingest: TextIngestModel,
                      user: User = Depends(get_current_username_project),
                      db: Session = Depends(get_db)):

    try:
        project = brain.findProject(projectName, db)

        if project.model.type != "rag":
            raise HTTPException(
                status_code=400, detail='{"error": "Only available for RAG projects."}')

        metadata = {"source": ingest.source}
        documents = [Document(text=ingest.text, metadata=metadata)]

        if ingest.keywords and len(ingest.keywords) > 0:
            for document in documents:
                document.metadata["keywords"] = ", ".join(ingest.keywords)
        else:
            documents = ExtractKeywordsForMetadata(documents)

        # for document in documents:
        #    document.text = document.text.decode('utf-8')

        nchunks = IndexDocuments(project, documents, ingest.splitter, ingest.chunks)
        project.vector.save()

        return {"source": ingest.source, "documents": len(documents), "chunks": nchunks}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.post("/projects/{projectName}/embeddings/ingest/url", response_model=IngestResponse)
async def ingest_url(projectName: str, ingest: URLIngestModel,
                     user: User = Depends(get_current_username_project),
                     db: Session = Depends(get_db)):
    try:
        if ingest.url and not ingest.url.startswith('http'):
            raise HTTPException(
                status_code=400, detail='{"error": "Specify the protocol http:// or https://"}')
      
        project = brain.findProject(projectName, db)

        if project.model.type != "rag":
            raise HTTPException(
                status_code=400, detail='{"error": "Only available for RAG projects."}')

        urls = project.vector.list()
        if (ingest.url in urls):
            raise Exception("URL already ingested. Delete first.")

        loader = SeleniumWebReader()

        documents = loader.load_data(urls=[ingest.url])
        documents = ExtractKeywordsForMetadata(documents)

        nchunks = IndexDocuments(project, documents, ingest.splitter, ingest.chunks)
        project.vector.save()

        return {"source": ingest.url, "documents": len(documents), "chunks": nchunks}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.post("/projects/{projectName}/embeddings/ingest/upload", response_model=IngestResponse)
async def ingest_file(
        projectName: str,
        file: UploadFile,
        options: str = Form("{}"),
        chunks: int = Form(256),
        splitter: str = Form("sentence"),
        user: User = Depends(get_current_username_project),
        db: Session = Depends(get_db)):
    try:
        project = brain.findProject(projectName, db)

        if project.model.type != "rag":
            raise HTTPException(
                status_code=400, detail='{"error": "Only available for RAG projects."}')

        _, ext = os.path.splitext(file.filename or '')
        temp = NamedTemporaryFile(delete=False, suffix=ext)
        try:
            contents = file.file.read()
            with temp as f:
                f.write(contents)
        except Exception:
            raise HTTPException(
                status_code=500, detail='{"error": "Error while saving file."}')
        finally:
            file.file.close()

        opts = json.loads(urllib.parse.unquote(options))

        loader = FindFileLoader(ext, opts)
        documents = loader.load_data(file=Path(temp.name))

        for document in documents:
            if "filename" in document.metadata:
                del document.metadata["filename"]
            document.metadata["source"] = file.filename

        documents = ExtractKeywordsForMetadata(documents)

        nchunks = IndexDocuments(project, documents, splitter, chunks)
        project.vector.save()

        return {
            "source": file.filename,
            "documents": len(documents), "chunks": nchunks}
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)

        raise HTTPException(
            status_code=500, detail=str(e))


@app.get('/projects/{projectName}/embeddings')
async def get_embeddings(
        projectName: str,
        user: User = Depends(get_current_username_project),
        db: Session = Depends(get_db)):
    project = brain.findProject(projectName, db)

    if project.model.type != "rag":
        raise HTTPException(
            status_code=400, detail='{"error": "Only available for RAG projects."}')

    if project.vector is not None:
        output = project.vector.list()
    else:
        output = []

    return {"embeddings": output}


@app.delete('/projects/{projectName}/embeddings/{source}')
async def delete_embedding(
        projectName: str,
        source: str,
        user: User = Depends(get_current_username_project),
        db: Session = Depends(get_db)):
    project = brain.findProject(projectName, db)

    if project.model.type != "rag":
        raise HTTPException(
            status_code=400, detail='{"error": "Only available for RAG projects."}')

    ids = project.vector.delete_source(base64.b64decode(source).decode('utf-8'))

    return {"deleted": len(ids)}


@app.post("/projects/{projectName}/chat")
async def chat_query(
        request: Request,
        projectName: str,
        input: ChatModel,
        user: User = Depends(get_current_username_project),
        db: Session = Depends(get_db)):
    try:
        if not input.question:
            raise HTTPException(
                status_code=400, detail='{"error": "Missing question"}')
      
        project = brain.findProject(projectName, db)
        if project is None:
            raise Exception("Project not found")

        return await chat_main(request, brain, project, input, user, db)
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))


@app.post("/projects/{projectName}/question")
async def question_query_endpoint(
        request: Request,
        projectName: str,
        input: QuestionModel,
        user: User = Depends(get_current_username_project),
        db: Session = Depends(get_db)):
    try:
        if not input.question:
            raise HTTPException(
                status_code=400, detail='{"error": "Missing question"}')
            
        project = brain.findProject(projectName, db)
        if project is None:
            raise Exception("Project not found")
            
        return await question_main(request, brain, project, input, user, db)
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e))

        
@app.post("/tools/classifier", response_model=ClassifierResponse)
async def classifier(
        input: ClassifierModel,
        user: User = Depends(get_current_username),
        db: Session = Depends(get_db)):
    try:
        return brain.classify(input)
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        raise HTTPException(
            status_code=500, detail=str(e)
)
        
       
@app.get("/tools/agent", response_model=list[Tool])
async def get_tools(
        user: User = Depends(get_current_username),
        db: Session = Depends(get_db)):
  
    _tools  = []
    
    for tool in brain.get_tools():
        _tools.append(Tool(name=tool.metadata.name, description=tool.metadata.description))
    
    return _tools

try:
    app.mount("/admin/", StaticFiles(directory="frontend/html/",
              html=True), name="static_admin")
    app.mount(
        "/admin/static/js",
        StaticFiles(
            directory="frontend/html/static/js"),
        name="static_js")
    app.mount(
        "/admin/static/css",
        StaticFiles(
            directory="frontend/html/static/css"),
        name="static_css")
    app.mount(
        "/admin/static/media",
        StaticFiles(
            directory="frontend/html/static/media"),
        name="static_media")
except BaseException:
    print("Admin frontend not available.")
