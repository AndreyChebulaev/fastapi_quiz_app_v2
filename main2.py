from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
import os
from pathlib import Path
from utils import parse_answers, format_answers, process_excel_file, save_excel_file, create_new_excel_file
import json
import shutil

app = FastAPI(title="Excel Questions Editor")

# Создаем директории
Path("uploads").mkdir(exist_ok=True)

# Настройка шаблонов
templates = Jinja2Templates(directory="templatesqq")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Только Excel файлы разрешены")
    
    # Сохраняем файл
    file_path = f"uploads/{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())
    
    # Обрабатываем файл
    try:
        original_data = process_excel_file(file_path)
        questions_data = []
        
        # Обрабатываем все строки как данные (без заголовков)
        for index, row in enumerate(original_data):
            if row and len(row) >= 2 and pd.notna(row[0]) and str(row[0]).strip():
                question = str(row[0])
                answers = parse_answers(row[1]) if pd.notna(row[1]) else [""]
                questions_data.append({
                    "index": index,
                    "question": question,
                    "answers": answers
                })
        
        return templates.TemplateResponse("edit.html", {
            "request": request,
            "filename": file.filename,
            "questions": questions_data,
            "original_data": json.dumps(original_data, ensure_ascii=False) if original_data else "[]"
        })
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка обработки файла: {str(e)}")

@app.post("/save")
async def save_file(
    request: Request,
    filename: str = Form(...),
    original_data: str = Form("[]"),
    questions: list[str] = Form(...),
    answers: list[str] = Form(...)
):
    try:
        # Определяем, это новый файл или редактирование существующего
        is_new_file = filename == "new_file.xlsx" or not original_data or original_data == "[]"
        
        if is_new_file:
            # СОЗДАНИЕ НОВОГО ФАЙЛА - БЕЗ ЗАГОЛОВКОВ
            output_filename = f"new_questions.xlsx"
            output_path = f"uploads/{output_filename}"
            
            # Подготавливаем данные для нового файла
            new_data = []
            for i in range(len(questions)):
                question_text = questions[i]
                # Парсим ответы из формы и правильно форматируем
                answers_list = [answer.strip() for answer in answers[i].split('","') if answer.strip()]
                # Убираем лишние кавычки
                clean_answers = [answer.strip('"') for answer in answers_list if answer.strip('"')]
                formatted_answers = format_answers(clean_answers)
                
                new_data.append({
                    "question": question_text,
                    "answers": formatted_answers
                })
            
            # Создаем новый файл БЕЗ заголовков
            create_new_excel_file(output_path, new_data)
            
            # Подготавливаем данные для отображения
            display_data = []
            for item in new_data:
                display_data.append({
                    "Question": item["question"],
                    "Answers": item["answers"]
                })
            
        else:
            # РЕДАКТИРОВАНИЕ СУЩЕСТВУЮЩЕГО ФАЙЛА
            # Полностью заменяем содержимое файла
            output_filename = f"edited_{filename}"
            output_path = f"uploads/{output_filename}"
            
            # Создаем новые данные
            new_data = []
            for i in range(len(questions)):
                question_text = questions[i]
                # Парсим ответы из формы и правильно форматируем
                answers_list = [answer.strip() for answer in answers[i].split('","') if answer.strip()]
                # Убираем лишние кавычки
                clean_answers = [answer.strip('"') for answer in answers_list if answer.strip('"')]
                formatted_answers = format_answers(clean_answers)
                
                new_data.append([question_text, formatted_answers])
            
            # Сохраняем полностью новый файл (не копируем старый)
            save_excel_file(output_path, new_data)
            
            # Подготавливаем данные для отображения
            display_data = []
            for row in new_data:
                display_data.append({
                    "Question": row[0] if len(row) > 0 else "",
                    "Answers": row[1] if len(row) > 1 else ""
                })
        
        return templates.TemplateResponse("view.html", {
            "request": request,
            "filename": output_filename,
            "data": display_data
        })
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сохранения: {str(e)}")

@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = f"uploads/{filename}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.get("/create-new")
async def create_new(request: Request):
    return templates.TemplateResponse("edit.html", {
        "request": request,
        "filename": "new_file.xlsx",
        "questions": [{"index": 0, "question": "", "answers": [""]}],
        "original_data": "[]"
    })