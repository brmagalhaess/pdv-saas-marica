from django.urls import path
from . import views,views_backup,views_fiscal,views_releases
urlpatterns=[
    path('company',views.company),
    path('integrations/<str:provider>',views.integration),
    path('jobs',views.jobs),path('billing/subscribe',views.subscribe),
    path('jobs/<uuid:pk>/reconcile',views.reconcile_job),
    path('webhooks/asaas',views.asaas_webhook),
    path('fiscal/config',views_fiscal.fiscal_config),
    path('fiscal/certificate',views_fiscal.fiscal_certificate),
    path('fiscal/test',views_fiscal.fiscal_test),
    path('fiscal/inutilize',views_fiscal.fiscal_inutilize),
    path('fiscal/submit',views_fiscal.fiscal_submit),path('fiscal/documents',views_fiscal.fiscal_documents),
    path('fiscal/documents/<uuid:pk>/xml',views_fiscal.fiscal_xml),
    path('fiscal/documents/<uuid:pk>/cancel',views_fiscal.fiscal_cancel),
    path('fiscal/documents/<uuid:pk>/retry',views_fiscal.fiscal_retry),
    path('fiscal/events/<uuid:pk>/xml',views_fiscal.fiscal_event_xml),
    path('fiscal/inutilizations/<uuid:pk>/xml',views_fiscal.fiscal_inutilization_xml),
    path('health',views.health),
    path('backups',views_backup.backups),
    path('platform/metrics',views.platform_metrics),
    path('releases',views_releases.releases),path('releases/status',views_releases.status),path('releases/current',views_releases.current),
    path('releases/<uuid:pk>/withdraw',views_releases.withdraw),
]
