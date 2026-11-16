"""Additive logical incident links; no old row migration or deletion."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('code_action_links',
        sa.Column('action_id', sa.String(64), primary_key=True),
        sa.Column('incident_id', sa.BigInteger, sa.ForeignKey('incidents.id'), nullable=False, unique=True),
        sa.Column('application_id', sa.String(64), nullable=False, unique=True),
        sa.Column('request_id', sa.String(128), unique=True),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('request', JSONB, nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('application_state', sa.String(64), nullable=False),
        sa.Column('projected_status', sa.String(32), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True)),
        sa.Column('state_changed_at', sa.DateTime(timezone=True)),
        sa.Column('observation', sa.String(32), nullable=False),
        sa.Column('created_by', sa.String(128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_code_action_links_incident_id', 'code_action_links', ['incident_id'])


def downgrade():
    raise RuntimeError('code-action history must be archived explicitly; destructive downgrade is disabled')
