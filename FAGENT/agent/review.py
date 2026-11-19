"""Module FAGENT.agent.review: Cơ chế xác thực và kiểm duyệt phản hồi từ Human Review.

Trách nhiệm & Chức năng:
- Cung cấp lớp `ReviewState` nhận diện chính xác ý định hoàn thành nhiệm vụ và cấp quyền lưu tri thức.
- Sử dụng Regex so khớp toàn chuỗi nghiêm ngặt với các mẫu câu chuẩn (tiếng Anh và tiếng Việt):
  Ví dụ: 'The task is complete. Save the knowledge from this run.', 'Task đã hoàn thành. Hãy lưu kiến thức...'
- Bảo vệ an toàn tri thức: Chỉ cấp quyền (authorized=True, save_requested=True) khi có phê duyệt rõ ràng;
  tự động thu hồi quyền nếu layout tiếp tục bị chỉnh sửa hoặc validate lại.
"""
import re


class ReviewState:
    def run(self, feedback):
        self.reset()
        self.evidence = {'feedback': feedback, 'completed': False, 'save_requested': False}
        normalized = ' '.join(feedback.casefold().strip().split())
        patterns = (
            r'(?:the )?task is complete(?:[.!]| and) (?:please )?save (?:the )?knowledge(?: from this run)?[.!]?',
            r'(?:task đã hoàn thành|hoàn thành rồi)(?:[.!]|,) (?:hãy )?lưu kiến thức(?: từ lần chạy này)?[.!]?',
        )
        if any(re.fullmatch(pattern, normalized) for pattern in patterns):
            self.authorized = True
            self.evidence.update(completed=True, save_requested=True)
        return self

    def __init__(self):
        self.authorized = False
        self.evidence = {}

    def reset(self):
        self.authorized = False
        self.evidence = {}
