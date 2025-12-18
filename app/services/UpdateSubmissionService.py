"""
S12 Partner Knowledge Update Service - UpdateSubmissionService

This service handles knowledge update submissions including:
- Creating draft updates by snapshotting from S11
- Deleting draft updates
- Submitting updates to Telcenter Core for validation via A34
"""

import os
import base64
import json
import uuid
import threading
from datetime import datetime
from typing import Any, Optional
from pymongo.collection import Collection
from flask_restx import abort

from .common.BaseCRUDService import BaseCRUDService
from .MessageQueueService import MessageQueueService
from ..utils.db import str_to_objectid, serialize_mongo_doc


class UpdateSubmissionService(BaseCRUDService):
    """
    Service for managing knowledge update submissions.
    
    Handles:
    - Creating draft updates (calls S11 via A33 for snapshot)
    - Deleting draft updates
    - Submitting updates to Core via A34
    """
    
    # Status constants
    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_VALIDATING = "validating"
    STATUS_VALIDATED = "validated"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    
    # Chunk size for A34 transmission (before base64 encoding)
    CHUNK_SIZE_BYTES = 256
    
    def __init__(self, collection: Collection):
        super().__init__(collection=collection, enable_timing=True)
        
        # Queue names from environment
        self.snapshot_requests_queue = os.getenv(
            "SNAPSHOT_REQUESTS_QUEUE_NAME", 
            "snapshot_requests"
        )
        self.snapshot_responses_queue = os.getenv(
            "SNAPSHOT_RESPONSES_QUEUE_NAME", 
            "snapshot_responses"
        )
        self.update_validation_event_queue = os.getenv(
            "UPDATE_VALIDATION_EVENT_QUEUE",
            "update_validation_events"
        )
        self.partner_id = os.getenv("PARTNER_ID", "default_partner")
        
        # MessageQueueService instance and lock for thread safety
        self._mq_service: Optional[MessageQueueService] = None
        self._mq_lock = threading.Lock()
        
        # Pending RPC responses storage
        self._pending_responses: dict[str, Any] = {}
        self._response_events: dict[str, threading.Event] = {}
        self._response_lock = threading.Lock()
        
        # Background listener thread
        self._listener_thread: Optional[threading.Thread] = None
        self._listener_started = False
    
    def _get_mq_service(self) -> MessageQueueService:
        """Get or create MessageQueueService instance (thread-safe)."""
        with self._mq_lock:
            if self._mq_service is None:
                self._mq_service = MessageQueueService()
            return self._mq_service
    
    def _start_response_listener(self):
        """Start background thread to listen for RPC responses."""
        if self._listener_started:
            return
        
        def _listen_for_responses():
            with self._mq_lock:
                mq = self._get_mq_service().clone()
            
            mq.declare_queue(self.snapshot_responses_queue)
            
            def handle_response(message: dict):
                request_id = message.get("id")
                if request_id:
                    with self._response_lock:
                        self._pending_responses[request_id] = message
                        if request_id in self._response_events:
                            self._response_events[request_id].set()
            
            mq.register_callback(self.snapshot_responses_queue, handle_response)
            mq.start_consuming()
        
        self._listener_thread = threading.Thread(
            target=_listen_for_responses, 
            daemon=True
        )
        self._listener_thread.start()
        self._listener_started = True
    
    def _call_s11_snapshot(self, timeout: float = 30.0) -> dict:
        """
        Call S11 via A33 to obtain a snapshot of current knowledge data.
        
        Returns:
            dict with 'seaweed_file_id' on success
            
        Raises:
            Exception on failure
        """
        self._start_response_listener()
        
        request_id = str(uuid.uuid4())
        
        # Create event for this request
        with self._response_lock:
            self._response_events[request_id] = threading.Event()
        
        # Send RPC request to S11
        request_message = {
            "method": "snapshot",
            "params": {},
            "id": request_id
        }
        
        with self._mq_lock:
            mq = self._get_mq_service().clone()
        mq.declare_queue(self.snapshot_requests_queue)
        mq.publish_message(self.snapshot_requests_queue, request_message)
        
        # Wait for response
        event = self._response_events[request_id]
        if not event.wait(timeout=timeout):
            # Cleanup
            with self._response_lock:
                self._response_events.pop(request_id, None)
                self._pending_responses.pop(request_id, None)
            raise TimeoutError("Timeout waiting for S11 snapshot response")
        
        # Get response
        with self._response_lock:
            response = self._pending_responses.pop(request_id, None)
            self._response_events.pop(request_id, None)
        
        if not response:
            raise Exception("No response received from S11")
        
        result = response.get("result", {})
        if result.get("status") != "success":
            error_content = result.get("content", "Unknown error")
            raise Exception(f"S11 snapshot failed: {error_content}")
        
        return result.get("content", {})
    
    def _send_a34_event(self, event_type: str, data: dict):
        """Send event to S05 via A34 (fire-and-forget)."""
        with self._mq_lock:
            mq = self._get_mq_service().clone()
        
        mq.declare_queue(self.update_validation_event_queue)
        
        event_message = {
            "event": event_type,
            "data": data
        }
        
        mq.publish_message(self.update_validation_event_queue, event_message)
    
    def _fetch_seaweed_file(self, file_id: str) -> bytes:
        """
        Fetch file content from SeaweedFS.
        
        Note: This is a placeholder implementation.
        In production, this should call the SeaweedFS HTTP API.
        """
        seaweed_url = os.getenv("SEAWEED_URL", "http://localhost:8080")
        
        import requests
        response = requests.get(f"{seaweed_url}/{file_id}")
        response.raise_for_status()
        return response.content
    
    def _send_snapshot_in_chunks(self, snapshot_id: str, file_content: bytes):
        """
        Send snapshot data to S05 in chunks via A34.
        
        Args:
            snapshot_id: Unique ID for this snapshot transmission
            file_content: Raw bytes of the snapshot file
        """
        # Send snapshot_start event
        self._send_a34_event("snapshot_start", {
            "partner_id": self.partner_id,
            "snapshot_id": snapshot_id
        })
        
        # Split into chunks and send
        total_chunks = (len(file_content) + self.CHUNK_SIZE_BYTES - 1) // self.CHUNK_SIZE_BYTES
        
        for seq in range(total_chunks):
            start = seq * self.CHUNK_SIZE_BYTES
            end = min(start + self.CHUNK_SIZE_BYTES, len(file_content))
            chunk_data = file_content[start:end]
            chunk_data_base64 = base64.b64encode(chunk_data).decode('utf-8')
            
            self._send_a34_event("snapshot_chunk", {
                "partner_id": self.partner_id,
                "snapshot_id": snapshot_id,
                "seq": seq + 1,  # 1-indexed
                "chunk_data_base64": chunk_data_base64
            })
        
        # Send snapshot_stop event
        self._send_a34_event("snapshot_stop", {
            "partner_id": self.partner_id,
            "snapshot_id": snapshot_id,
            "total_chunks": total_chunks
        })
    
    def create_draft_update(
        self, 
        source: str,
        update_name: str,
        update_type: str,
        priority: str,
        notes: Optional[str],
        created_by: str
    ) -> dict:
        """
        Create a new knowledge update draft.
        
        Flow (H29 → S12 → S11 via A33):
        1. Call S11 via A33 to snapshot current knowledge data
        2. S11 stores snapshot in SeaweedFS, returns file ID
        3. Create draft record in database
        4. Return draft details
        
        Args:
            source: Source of the update (e.g., "Viettel")
            update_name: Name/description of the update
            update_type: Type of update (new_entries, modifications, corrections)
            priority: Priority level (high, normal, low)
            notes: Optional notes about the update
            created_by: ID of the user creating the update
            
        Returns:
            dict with update details
            
        Raises:
            Exception on failure
        """
        print(f"Creating draft update: {update_name} by {created_by}")
        # Validate update_type
        valid_update_types = ["new_entries", "modifications", "corrections"]
        if update_type not in valid_update_types:
            abort(400, f"Invalid update_type. Must be one of: {valid_update_types}")
        
        print(f"Update type validated: {update_type}")

        # Validate priority
        valid_priorities = ["high", "normal", "low"]
        if priority not in valid_priorities:
            abort(400, f"Invalid priority. Must be one of: {valid_priorities}")
        
        print(f"Priority validated: {priority}")

        try:
            # Step 1: Call S11 to get snapshot
            snapshot_result = self._call_s11_snapshot()
            seaweed_file_id = snapshot_result.get("seaweed_file_id")

            print(f"Received seaweed_file_id: {seaweed_file_id}")
            
            if not seaweed_file_id:
                raise Exception("S11 did not return seaweed_file_id")
            
            # Step 2: Count entries (fetch file and parse to count)
            print(f"Fetching snapshot file from SeaweedFS: {seaweed_file_id}")
            try:
                file_content = self._fetch_seaweed_file(seaweed_file_id)
                data = json.loads(file_content)
                # Count packages and FAQs
                entry_count = len(data.get("packages", [])) + len(data.get("faqs", []))
            except Exception:
                entry_count = 0  # Default if can't fetch/parse
            
            print(f"Counted {entry_count} entries in snapshot")
            # Step 3: Create draft record
            now = datetime.utcnow()
            draft_doc = {
                "update_name": update_name,
                "source": source,
                "update_type": update_type,
                "priority": priority,
                "notes": notes,
                "entry_count": entry_count,
                "seaweed_file_id": seaweed_file_id,
                "status": self.STATUS_DRAFT,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
                "submitted_at": None,
                "response_at": None,
                "result_message": None,
            }


            print(f"Inserting draft document into database: {draft_doc}")
            
            result = self.collection.insert_one(draft_doc)
            draft_doc["_id"] = result.inserted_id

            print(f"Draft update created with ID: {draft_doc['_id']}")
            
            return serialize_mongo_doc(draft_doc)
            
        except TimeoutError as e:
            abort(503, f"Không thể kết nối đến dịch vụ S11: {str(e)}")
        except Exception as e:
            abort(500, f"Lỗi khi tạo bản cập nhật: {str(e)}")
    
    def delete_draft_update(self, update_id: str, deleted_by: str) -> dict:
        """
        Delete a draft update.
        
        Only updates in 'draft' status can be deleted.
        
        Args:
            update_id: ID of the update to delete
            deleted_by: ID of the user deleting the update
            
        Returns:
            dict with deleted update details
            
        Raises:
            404 if update not found
            400 if update is not in draft status
        """
        object_id = str_to_objectid(update_id)
        if not object_id:
            abort(404, "Không tìm thấy bản cập nhật với ID này")
        
        # Find the update
        update_doc = self.collection.find_one({"_id": object_id})
        if not update_doc:
            abort(404, "Không tìm thấy bản cập nhật với ID này")
        
        # Check status
        if update_doc.get("status") != self.STATUS_DRAFT:
            abort(400, 
                  message="Không thể xóa bản cập nhật",
                  details="Chỉ có thể xóa bản cập nhật ở trạng thái draft",
                  current_status=update_doc.get("status"))
        
        # Delete the update
        self.collection.delete_one({"_id": object_id})
        
        # Return deleted update info
        deleted_info = serialize_mongo_doc(update_doc)
        deleted_info["deleted_at"] = datetime.utcnow().isoformat()
        deleted_info["deleted_by"] = deleted_by
        
        return deleted_info
    
    def submit_to_core(
        self, 
        update_id: str, 
        submission_notes: Optional[str] = None,
        expected_validation_time: Optional[str] = None
    ) -> dict:
        """
        Submit a draft update to Telcenter Core for validation.
        
        Flow:
        1. Retrieve draft update from database
        2. Fetch snapshot file from SeaweedFS
        3. Send to S05 via A34 in chunks
        4. Update status to 'pending'
        
        Args:
            update_id: ID of the update to submit
            submission_notes: Optional notes for submission
            expected_validation_time: Optional expected time (urgent, normal)
            
        Returns:
            dict with submission details
            
        Raises:
            404 if update not found
            400 if update is not in draft status
            503 if Core is unreachable
        """
        object_id = str_to_objectid(update_id)
        if not object_id:
            abort(404, "Không tìm thấy bản cập nhật với ID này")
        
        # Find the update
        update_doc = self.collection.find_one({"_id": object_id})
        if not update_doc:
            abort(404, "Không tìm thấy bản cập nhật với ID này")
        
        # Check status
        if update_doc.get("status") != self.STATUS_DRAFT:
            abort(400,
                  message="Không thể gửi bản cập nhật",
                  details="Chỉ có thể gửi bản cập nhật ở trạng thái draft",
                  current_status=update_doc.get("status"))
        
        try:
            # Get seaweed file
            seaweed_file_id = update_doc.get("seaweed_file_id")
            if not seaweed_file_id:
                abort(400, "Bản cập nhật không có dữ liệu snapshot")
            
            # Fetch file content from SeaweedFS
            file_content = self._fetch_seaweed_file(seaweed_file_id)
            
            # Generate submission/snapshot ID
            snapshot_id = str(uuid.uuid4())
            submission_id = f"partner_sub_{uuid.uuid4().hex[:8]}"
            
            # Send to S05 via A34 in chunks
            self._send_snapshot_in_chunks(snapshot_id, file_content)
            
            # Update status to pending/validating
            now = datetime.utcnow()
            self.collection.update_one(
                {"_id": object_id},
                {
                    "$set": {
                        "status": self.STATUS_PENDING,
                        "submitted_at": now,
                        "submission_id": submission_id,
                        "snapshot_id": snapshot_id,
                        "submission_notes": submission_notes,
                        "updated_at": now,
                    }
                }
            )
            
            # Calculate estimated validation time
            estimated_time = 120  # Default 2 minutes
            if expected_validation_time == "urgent":
                estimated_time = 60
            
            return {
                "status": "submitted",
                "update_id": update_id,
                "submission_id": submission_id,
                "message": "Đã gửi bản cập nhật lên Telcenter Core để validation",
                "submitted_at": now.isoformat(),
                "estimated_validation_time_seconds": estimated_time,
                "status_url": f"/api/v1/updates/{update_id}/status"
            }
            
        except Exception as e:
            if "Connection" in str(e) or "refused" in str(e).lower():
                abort(503, 
                      message="Không thể kết nối đến Telcenter Core",
                      details="Vui lòng thử lại sau",
                      retry_after_seconds=60)
            raise
    
    def get_update_status(self, update_id: str) -> dict:
        """
        Get the current status of an update submission.
        
        Args:
            update_id: ID of the update
            
        Returns:
            dict with current status details
        """
        object_id = str_to_objectid(update_id)
        if not object_id:
            abort(404, "Không tìm thấy bản cập nhật với ID này")
        
        update_doc = self.collection.find_one({"_id": object_id})
        if not update_doc:
            abort(404, "Không tìm thấy bản cập nhật với ID này")
        
        status = update_doc.get("status")
        result = serialize_mongo_doc(update_doc)
        
        # Add appropriate message based on status
        if status == self.STATUS_DRAFT:
            result["message"] = "Bản cập nhật đang ở trạng thái draft"
        elif status == self.STATUS_PENDING:
            result["message"] = "Đang chờ validation từ Telcenter Core"
        elif status == self.STATUS_VALIDATING:
            result["message"] = "Đang chờ validation từ Telcenter Core"
            result["current_stage"] = "core_validation"
        elif status == self.STATUS_APPROVED:
            result["message"] = "Bản cập nhật đã được Core phê duyệt và đồng bộ vào local"
        elif status == self.STATUS_REJECTED:
            result["message"] = "Validation thất bại"
        
        return result
    
    def get_all_updates(self, status_filter: Optional[str] = None) -> list:
        """
        Get all updates, optionally filtered by status.
        
        Args:
            status_filter: Optional status to filter by
            
        Returns:
            list of update documents
        """
        query = {}
        if status_filter:
            query["status"] = status_filter
        
        updates = list(self.collection.find(query).sort("updated_at", -1))
        return [serialize_mongo_doc(doc) for doc in updates]
