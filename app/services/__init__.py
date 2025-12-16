from .ConversationService import ConversationService
from .UpdateSubmissionService import UpdateSubmissionService
from ..collections import conversations_collection, submissions_collection

conversation_service = ConversationService(
    collection=conversations_collection,
)

update_submission_service = UpdateSubmissionService(
    collection=submissions_collection,
)
