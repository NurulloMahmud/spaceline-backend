from rest_framework.routers import SimpleRouter
from .views import (StatementStatusViewSet,
                    DeductionTypeViewSet
                    )

router = SimpleRouter()
router.register(r'statement-statuses', StatementStatusViewSet, basename='statement-statuses') #checked
router.register(r'deduction-types', DeductionTypeViewSet, basename='deduction-types') #checked

urlpatterns = [
]

urlpatterns += router.urls
