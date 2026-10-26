"""Errors shared by attachment conversion, lifecycle and generation."""

class AttachmentError(Exception):
    def __init__(self, message, code='INVALID_ATTACHMENT', status=400):
        super().__init__(message)
        self.code, self.status = code, status
