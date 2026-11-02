"""model 模块定义机器人问答的审计表。"""
from datetime import datetime

from extensions import db


class ChatbotQuestion(db.Model):
    """记录一次群内代码问答的输入、输出、耗时与状态，便于排查和统计。"""

    __tablename__ = "chatbot_questions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 任务标识：与网关生成的任务号一致，便于跨进程串联日志。
    task_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # 会话标识：群聊为 chatid，单聊为 userid。
    chatid = db.Column(db.String(128), index=True)
    # 会话类型：group / single。
    chattype = db.Column(db.String(16))
    # 提问者 userid；非超管创建的机器人拿到的是加密 userid。
    userid = db.Column(db.String(128), index=True)
    # 提问时选中的项目标识与展示名。
    project_key = db.Column(db.String(64), index=True)
    project_name = db.Column(db.String(128))
    # 原始问题与模型答案。
    question = db.Column(db.Text)
    answer = db.Column(db.Text)
    # 执行状态：running / succeeded / failed。
    status = db.Column(db.String(20), nullable=False, default="running", index=True)
    # 失败原因，成功时为空。
    error = db.Column(db.Text)
    # 端到端耗时（毫秒）。
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    finished_at = db.Column(db.DateTime)

    def to_dict(self):
        """转换为接口返回用的字典结构。"""
        return {
            "task_id": self.task_id,
            "chatid": self.chatid,
            "chattype": self.chattype,
            "userid": self.userid,
            "project_key": self.project_key,
            "project_name": self.project_name,
            "question": self.question,
            "answer": self.answer,
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def __repr__(self):
        """返回便于日志记录和调试查看的对象表示。"""
        return f"<ChatbotQuestion {self.task_id} {self.status}>"
