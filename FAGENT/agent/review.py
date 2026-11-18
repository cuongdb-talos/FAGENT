"""Conservative recognition of explicit human review authorization."""

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
