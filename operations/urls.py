from django.urls import path
from . import views,views_releases
urlpatterns=[
    path('company',views.company),
    path('integrations/<str:provider>',views.integration),
    path('jobs',views.jobs),path('billing/subscribe',views.subscribe),
    path('jobs/<uuid:pk>/reconcile',views.reconcile_job),
    path('webhooks/asaas',views.asaas_webhook),
    path('fiscal/submit',views.fiscal_submit),path('fiscal/documents',views.fiscal_documents),
    path('fiscal/documents/<uuid:pk>/xml',views.fiscal_xml),
    path('fiscal/documents/<uuid:pk>/cancel',views.fiscal_cancel),
    path('health',views.health),
    path('platform/metrics',views.platform_metrics),
    path('releases',views_releases.releases),path('releases/status',views_releases.status),path('releases/current',views_releases.current),
    path('releases/<uuid:pk>/withdraw',views_releases.withdraw),
]
