from app.ai.spam_detector import detect_spam
import json
print(detect_spam({"description": "Short desc", "title": "Software Engineer", "job_url": "foo"}))
print(detect_spam({"description": "x"*500, "title": "Software Engineer", "job_url": "foo"}))
