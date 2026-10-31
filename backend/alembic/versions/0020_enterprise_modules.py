"""enterprise modules schema

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-30 17:40:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "domain_analysis_records",
    "content_templates",
    "link_prospects",
    "ppc_keywords",
    "social_accounts",
    "social_posts",
    "seo_diagnoses",
    "code_repositories",
    "code_fix_proposals",
)


def upgrade() -> None:
    op.add_column('audit_findings', sa.Column('status', sa.String(length=16), server_default='open', nullable=False))
    op.add_column('audit_findings', sa.Column('category', sa.String(length=32), server_default='technical', nullable=False))
    op.add_column('audit_findings', sa.Column('root_cause', sa.Text(), nullable=True))
    op.add_column('audit_findings', sa.Column('seo_impact', sa.Text(), nullable=True))
    op.add_column('audit_findings', sa.Column('priority', sa.Integer(), server_default='50', nullable=False))

    op.create_table(
        'domain_analysis_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('domain', sa.String(length=253), nullable=False),
        sa.Column('database', sa.String(length=64), nullable=False),
        sa.Column('overview_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('technical_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('data_source', sa.String(length=64), nullable=False),
        sa.Column('source_class', sa.String(length=32), nullable=False),
        sa.Column('is_estimated', sa.Boolean(), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('confidence_level', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_dom_analysis_org_domain', 'domain_analysis_records', ['org_id', 'domain'], unique=False)
    op.create_index(op.f('ix_domain_analysis_records_org_id'), 'domain_analysis_records', ['org_id'], unique=False)
    op.create_index(op.f('ix_domain_analysis_records_domain'), 'domain_analysis_records', ['domain'], unique=False)

    op.create_table(
        'content_templates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('keyword', sa.String(length=300), nullable=False),
        sa.Column('database', sa.String(length=64), nullable=False),
        sa.Column('target_url', sa.Text(), nullable=True),
        sa.Column('template_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('onpage_check', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('data_source', sa.String(length=64), nullable=False),
        sa.Column('source_class', sa.String(length=32), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_content_templates_org_id'), 'content_templates', ['org_id'], unique=False)
    op.create_index(op.f('ix_content_templates_keyword'), 'content_templates', ['keyword'], unique=False)

    op.create_table(
        'link_prospects',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('domain', sa.String(length=253), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('target_url', sa.Text(), nullable=True),
        sa.Column('link_type', sa.String(length=32), nullable=False),
        sa.Column('relevance_score', sa.Float(), nullable=False),
        sa.Column('domain_strength', sa.Float(), nullable=True),
        sa.Column('opportunity_score', sa.Float(), nullable=False),
        sa.Column('contact_info', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('outreach_status', sa.String(length=32), nullable=False),
        sa.Column('email_draft', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('outreach_history', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('follow_up_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('data_source', sa.String(length=64), nullable=False),
        sa.Column('source_class', sa.String(length=32), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', 'source_url', 'target_url', name='uq_prospect_org_urls')
    )
    op.create_index(op.f('ix_link_prospects_org_id'), 'link_prospects', ['org_id'], unique=False)
    op.create_index(op.f('ix_link_prospects_site_id'), 'link_prospects', ['site_id'], unique=False)
    op.create_index(op.f('ix_link_prospects_domain'), 'link_prospects', ['domain'], unique=False)
    op.create_index(op.f('ix_link_prospects_opportunity_score'), 'link_prospects', ['opportunity_score'], unique=False)
    op.create_index(op.f('ix_link_prospects_outreach_status'), 'link_prospects', ['outreach_status'], unique=False)

    op.create_table(
        'ppc_keywords',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('domain', sa.String(length=253), nullable=False),
        sa.Column('keyword', sa.String(length=500), nullable=False),
        sa.Column('keyword_key', sa.String(length=500), nullable=False),
        sa.Column('database', sa.String(length=64), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('search_volume', sa.Integer(), nullable=True),
        sa.Column('estimated_cpc', sa.Float(), nullable=True),
        sa.Column('competition', sa.Float(), nullable=True),
        sa.Column('ad_headline', sa.String(length=500), nullable=True),
        sa.Column('ad_description', sa.Text(), nullable=True),
        sa.Column('landing_page', sa.Text(), nullable=True),
        sa.Column('estimated_monthly_clicks', sa.Float(), nullable=True),
        sa.Column('estimated_monthly_cost', sa.Float(), nullable=True),
        sa.Column('is_estimated', sa.Boolean(), nullable=False),
        sa.Column('data_source', sa.String(length=64), nullable=False),
        sa.Column('source_class', sa.String(length=32), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ppc_org_domain', 'ppc_keywords', ['org_id', 'domain'], unique=False)
    op.create_index(op.f('ix_ppc_keywords_org_id'), 'ppc_keywords', ['org_id'], unique=False)
    op.create_index(op.f('ix_ppc_keywords_domain'), 'ppc_keywords', ['domain'], unique=False)
    op.create_index(op.f('ix_ppc_keywords_keyword_key'), 'ppc_keywords', ['keyword_key'], unique=False)

    op.create_table(
        'social_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('handle', sa.String(length=200), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('encrypted_access_token', sa.Text(), nullable=True),
        sa.Column('encrypted_refresh_token', sa.Text(), nullable=True),
        sa.Column('scopes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', 'platform', 'handle', name='uq_social_org_platform_handle')
    )
    op.create_index(op.f('ix_social_accounts_org_id'), 'social_accounts', ['org_id'], unique=False)

    op.create_table(
        'social_posts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('platforms', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('hashtags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('media_urls', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('source_page_url', sa.Text(), nullable=True),
        sa.Column('target_keyword', sa.String(length=300), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('user_approved', sa.Boolean(), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('external_post_url', sa.Text(), nullable=True),
        sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['social_accounts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_social_posts_org_id'), 'social_posts', ['org_id'], unique=False)
    op.create_index(op.f('ix_social_posts_site_id'), 'social_posts', ['site_id'], unique=False)
    op.create_index(op.f('ix_social_posts_status'), 'social_posts', ['status'], unique=False)

    op.create_table(
        'seo_diagnoses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('problem', sa.String(length=400), nullable=False),
        sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('affected_url', sa.Text(), nullable=True),
        sa.Column('source_module', sa.String(length=64), nullable=False),
        sa.Column('data_source', sa.String(length=64), nullable=False),
        sa.Column('source_class', sa.String(length=32), nullable=False),
        sa.Column('root_cause', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('seo_impact', sa.Text(), nullable=False),
        sa.Column('recommended_fix', sa.Text(), nullable=False),
        sa.Column('implementation_steps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('estimated_effort', sa.String(length=16), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('verification_method', sa.Text(), nullable=False),
        sa.Column('affected_file', sa.Text(), nullable=True),
        sa.Column('priority_score', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', 'fingerprint', name='uq_diagnosis_site_fp')
    )
    op.create_index(op.f('ix_seo_diagnoses_org_id'), 'seo_diagnoses', ['org_id'], unique=False)
    op.create_index(op.f('ix_seo_diagnoses_site_id'), 'seo_diagnoses', ['site_id'], unique=False)
    op.create_index(op.f('ix_seo_diagnoses_priority_score'), 'seo_diagnoses', ['priority_score'], unique=False)
    op.create_index(op.f('ix_seo_diagnoses_status'), 'seo_diagnoses', ['status'], unique=False)

    op.create_table(
        'code_repositories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('repo_type', sa.String(length=16), nullable=False),
        sa.Column('local_path', sa.Text(), nullable=True),
        sa.Column('github_repo', sa.String(length=300), nullable=True),
        sa.Column('default_branch', sa.String(length=64), nullable=False),
        sa.Column('detected_framework', sa.String(length=64), nullable=True),
        sa.Column('last_scanned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('scan_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_code_repositories_org_id'), 'code_repositories', ['org_id'], unique=False)
    op.create_index(op.f('ix_code_repositories_site_id'), 'code_repositories', ['site_id'], unique=False)

    op.create_table(
        'code_fix_proposals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('repo_id', sa.Integer(), nullable=False),
        sa.Column('diagnosis_id', sa.Integer(), nullable=True),
        sa.Column('finding_rule_id', sa.String(length=64), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('file_path', sa.Text(), nullable=False),
        sa.Column('line_start', sa.Integer(), nullable=False),
        sa.Column('line_end', sa.Integer(), nullable=False),
        sa.Column('issue_description', sa.Text(), nullable=False),
        sa.Column('root_cause', sa.Text(), nullable=False),
        sa.Column('seo_impact', sa.Text(), nullable=False),
        sa.Column('original_snippet', sa.Text(), nullable=False),
        sa.Column('proposed_snippet', sa.Text(), nullable=False),
        sa.Column('unified_diff', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('user_approved', sa.Boolean(), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('verification_report', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['diagnosis_id'], ['seo_diagnoses.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['repo_id'], ['code_repositories.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_code_fix_proposals_org_id'), 'code_fix_proposals', ['org_id'], unique=False)
    op.create_index(op.f('ix_code_fix_proposals_site_id'), 'code_fix_proposals', ['site_id'], unique=False)
    op.create_index(op.f('ix_code_fix_proposals_repo_id'), 'code_fix_proposals', ['repo_id'], unique=False)
    op.create_index(op.f('ix_code_fix_proposals_status'), 'code_fix_proposals', ['status'], unique=False)

    for t in TENANT_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} USING (app_org_id() IS NULL OR org_id = app_org_id()) "
            f"WITH CHECK (app_org_id() IS NULL OR org_id = app_org_id())"
        )


def downgrade() -> None:
    for t in reversed(TENANT_TABLES):
        op.drop_table(t)
    op.drop_column('audit_findings', 'priority')
    op.drop_column('audit_findings', 'seo_impact')
    op.drop_column('audit_findings', 'root_cause')
    op.drop_column('audit_findings', 'category')
    op.drop_column('audit_findings', 'status')
