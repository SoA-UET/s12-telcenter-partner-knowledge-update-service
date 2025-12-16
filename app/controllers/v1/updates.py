"""
H29 API Controller - Partner Knowledge Update Service (S12)

This controller implements the H29 HTTP API for:
- Creating new knowledge update drafts
- Deleting draft updates
- Submitting updates to Telcenter Core for validation
- Checking update status

All endpoints require JWT authentication.
"""

from flask import request, url_for, g
from flask_restx import Namespace, Resource, fields
from ...utils.hateoas import HATEOAS
from ...utils.auth import jwt_required, permission_required
from ..common.models import timing_enabled

#######################################
## STEP 1. DECLARE THE API NAMESPACE ##
#######################################

api = Namespace(
    'updates', 
    'API cho việc quản lý cập nhật dữ liệu kiến thức viễn thông của Partner.'
)

####################################
## STEP 2. DEFINE THE MODELS/DTOs ##
####################################

# Request DTOs
update_create_dto = api.model("UpdateCreate", {
    "source": fields.String(
        required=True, 
        description="Nguồn cập nhật (tên đối tác)",
        example="Viettel"
    ),
    "update_name": fields.String(
        required=True, 
        description="Tên bản cập nhật",
        example="Cập nhật gói cước Q4 2025"
    ),
    "update_type": fields.String(
        required=True, 
        description="Loại cập nhật",
        enum=["new_entries", "modifications", "corrections"],
        example="new_entries"
    ),
    "priority": fields.String(
        required=True, 
        description="Mức độ ưu tiên",
        enum=["high", "normal", "low"],
        example="normal"
    ),
    "notes": fields.String(
        required=False, 
        description="Ghi chú về bản cập nhật",
        example="Gói cước khuyến mãi quý 4 năm 2025"
    ),
})

update_submit_dto = api.model("UpdateSubmit", {
    "submission_notes": fields.String(
        required=False,
        description="Ghi chú khi gửi",
        example="Vui lòng xem xét và phê duyệt"
    ),
    "expected_validation_time": fields.String(
        required=False,
        description="Thời gian validation mong muốn",
        enum=["urgent", "normal"],
        example="normal"
    ),
})

# Response DTOs
update_detail_dto = api.model("UpdateDetail", {
    "id": fields.String(description="ID của bản cập nhật"),
    "update_name": fields.String(description="Tên bản cập nhật"),
    "source": fields.String(description="Nguồn cập nhật"),
    "update_type": fields.String(description="Loại cập nhật"),
    "priority": fields.String(description="Mức độ ưu tiên"),
    "entry_count": fields.Integer(description="Số lượng entries"),
    "seaweed_file_id": fields.String(description="ID file trên SeaweedFS"),
    "status": fields.String(description="Trạng thái bản cập nhật"),
    "created_at": fields.String(description="Thời gian tạo"),
    "created_by": fields.String(description="Người tạo"),
})

update_create_response_dto = api.model("UpdateCreateResponse", {
    "status": fields.String(description="Trạng thái response", example="success"),
    "update_id": fields.String(description="ID của bản cập nhật vừa tạo"),
    "update_status": fields.String(description="Trạng thái bản cập nhật", example="draft"),
    "message": fields.String(description="Thông báo"),
    "update": fields.Nested(update_detail_dto),
})

update_delete_response_dto = api.model("UpdateDeleteResponse", {
    "status": fields.String(description="Trạng thái response", example="success"),
    "message": fields.String(description="Thông báo"),
    "deleted_update": fields.Nested(api.model("DeletedUpdate", {
        "id": fields.String(description="ID của bản cập nhật"),
        "update_name": fields.String(description="Tên bản cập nhật"),
        "status": fields.String(description="Trạng thái trước khi xóa"),
        "entry_count": fields.Integer(description="Số lượng entries"),
        "deleted_at": fields.String(description="Thời gian xóa"),
        "deleted_by": fields.String(description="Người xóa"),
    })),
})

update_submit_response_dto = api.model("UpdateSubmitResponse", {
    "status": fields.String(description="Trạng thái", example="submitted"),
    "update_id": fields.String(description="ID bản cập nhật"),
    "submission_id": fields.String(description="ID lần submit"),
    "message": fields.String(description="Thông báo"),
    "submitted_at": fields.String(description="Thời gian gửi"),
    "estimated_validation_time_seconds": fields.Integer(description="Thời gian validation ước tính (giây)"),
    "status_url": fields.String(description="URL để kiểm tra trạng thái"),
})

update_status_response_dto = api.model("UpdateStatusResponse", {
    "status": fields.String(description="Trạng thái hiện tại"),
    "id": fields.String(description="ID bản cập nhật"),
    "submission_id": fields.String(description="ID lần submit"),
    "submitted_at": fields.String(description="Thời gian gửi"),
    "message": fields.String(description="Thông báo trạng thái"),
    "current_stage": fields.String(description="Giai đoạn hiện tại (nếu đang validate)"),
    "progress_percentage": fields.Integer(description="Phần trăm hoàn thành"),
})

error_response_dto = api.model("ErrorResponse", {
    "status": fields.String(description="Trạng thái", example="error"),
    "error_code": fields.String(description="Mã lỗi"),
    "message": fields.String(description="Thông báo lỗi"),
    "details": fields.String(description="Chi tiết lỗi (nếu có)"),
})

##################################
## STEP 3. CONNECT THE SERVICES ##
##################################

from ...services.UpdateSubmissionService import UpdateSubmissionService
from ...collections import submissions_collection

update_submission_service = UpdateSubmissionService(
    collection=submissions_collection
)

###################################
## STEP 4. DEFINE THE CONTROLLER ##
###################################

h = HATEOAS(api)


@api.route("/create")
class CreateUpdate(Resource):
    """Tạo bản cập nhật dữ liệu mới (draft)"""
    
    @api.doc(
        description="Tạo bản cập nhật dữ liệu kiến thức mới. "
                    "Hệ thống sẽ lấy snapshot từ S11 và lưu vào SeaweedFS.",
        security="Bearer"
    )
    @api.expect(update_create_dto)
    @api.response(201, "Tạo thành công", update_create_response_dto)
    @api.response(400, "Dữ liệu không hợp lệ", error_response_dto)
    @api.response(401, "Không có quyền truy cập", error_response_dto)
    @api.response(403, "Không có quyền tạo bản cập nhật", error_response_dto)
    @jwt_required
    def post(self):
        """Tạo bản cập nhật draft mới"""
        data = request.get_json()
        
        # Validate required fields
        required_fields = ["source", "update_name", "update_type", "priority"]
        for field in required_fields:
            if not data.get(field):
                return {
                    "status": "error",
                    "error_code": "INVALID_INPUT",
                    "message": f"Thiếu trường bắt buộc: {field}"
                }, 400
        
        # Create draft update
        update_doc = update_submission_service.create_draft_update(
            source=data["source"],
            update_name=data["update_name"],
            update_type=data["update_type"],
            priority=data["priority"],
            notes=data.get("notes"),
            created_by=g.user_id
        )
        
        return {
            "status": "success",
            "update_id": update_doc["id"],
            "update_status": "draft",
            "message": "Đã tạo bản cập nhật draft thành công",
            "update": update_doc
        }, 201


@api.route("/<string:update_id>")
@api.param("update_id", "ID của bản cập nhật")
class UpdateItem(Resource):
    """Quản lý một bản cập nhật cụ thể"""
    
    @api.doc(
        description="Xóa bản cập nhật đang ở trạng thái draft. "
                    "Chỉ có thể xóa bản cập nhật chưa được submit.",
        security="Bearer"
    )
    @api.response(200, "Xóa thành công", update_delete_response_dto)
    @api.response(400, "Không thể xóa", error_response_dto)
    @api.response(401, "Không có quyền truy cập", error_response_dto)
    @api.response(403, "Không có quyền xóa", error_response_dto)
    @api.response(404, "Không tìm thấy bản cập nhật", error_response_dto)
    @jwt_required
    def delete(self, update_id: str):
        """Xóa bản cập nhật draft"""
        try:
            deleted_update = update_submission_service.delete_draft_update(
                update_id=update_id,
                deleted_by=g.user_id
            )
            
            return {
                "status": "success",
                "message": "Đã xóa bản cập nhật draft thành công",
                "deleted_update": {
                    "id": deleted_update.get("id"),
                    "update_name": deleted_update.get("update_name"),
                    "status": deleted_update.get("status"),
                    "entry_count": deleted_update.get("entry_count"),
                    "deleted_at": deleted_update.get("deleted_at"),
                    "deleted_by": deleted_update.get("deleted_by"),
                }
            }, 200
        except Exception as e:
            error_msg = str(e)
            if "draft" in error_msg.lower():
                return {
                    "status": "error",
                    "error_code": "CANNOT_DELETE",
                    "message": "Không thể xóa bản cập nhật",
                    "details": "Chỉ có thể xóa bản cập nhật ở trạng thái draft"
                }, 400
            raise


@api.route("/<string:update_id>/submit")
@api.param("update_id", "ID của bản cập nhật")
class SubmitUpdate(Resource):
    """Gửi bản cập nhật lên Telcenter Core để validation"""
    
    @api.doc(
        description="Gửi bản cập nhật lên Telcenter Core để validation. "
                    "Chỉ có thể gửi bản cập nhật đang ở trạng thái draft.",
        security="Bearer"
    )
    @api.expect(update_submit_dto)
    @api.response(202, "Đã gửi thành công", update_submit_response_dto)
    @api.response(400, "Không thể gửi", error_response_dto)
    @api.response(401, "Không có quyền truy cập", error_response_dto)
    @api.response(403, "Không có quyền gửi", error_response_dto)
    @api.response(404, "Không tìm thấy bản cập nhật", error_response_dto)
    @api.response(503, "Không thể kết nối đến Core", error_response_dto)
    @jwt_required
    def post(self, update_id: str):
        """Gửi bản cập nhật lên Core để validation"""
        data = request.get_json() or {}
        
        result = update_submission_service.submit_to_core(
            update_id=update_id,
            submission_notes=data.get("submission_notes"),
            expected_validation_time=data.get("expected_validation_time")
        )
        
        return result, 202


@api.route("/<string:update_id>/status")
@api.param("update_id", "ID của bản cập nhật")
class UpdateStatus(Resource):
    """Kiểm tra trạng thái bản cập nhật"""
    
    @api.doc(
        description="Lấy trạng thái hiện tại của bản cập nhật. "
                    "Sử dụng endpoint này để poll trạng thái trong quá trình validation.",
        security="Bearer"
    )
    @api.response(200, "Trạng thái bản cập nhật", update_status_response_dto)
    @api.response(401, "Không có quyền truy cập", error_response_dto)
    @api.response(404, "Không tìm thấy bản cập nhật", error_response_dto)
    @jwt_required
    def get(self, update_id: str):
        """Lấy trạng thái bản cập nhật"""
        return update_submission_service.get_update_status(update_id), 200


@api.route("/")
class UpdateList(Resource):
    """Danh sách các bản cập nhật"""
    
    @api.doc(
        description="Lấy danh sách tất cả các bản cập nhật. "
                    "Có thể lọc theo trạng thái.",
        security="Bearer"
    )
    @api.param("status", "Lọc theo trạng thái (draft, pending, validated, rejected)", _in="query")
    @api.response(200, "Danh sách bản cập nhật")
    @api.response(401, "Không có quyền truy cập", error_response_dto)
    @jwt_required
    def get(self):
        """Lấy danh sách bản cập nhật"""
        status_filter = request.args.get("status")
        updates = update_submission_service.get_all_updates(status_filter=status_filter)
        
        return {
            "status": "success",
            "updates": updates,
            "total": len(updates)
        }, 200
