"""Read the fields needed by an Application incident summary from PostgreSQL."""
from sqlalchemy import case, cast, func, literal, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DataError


def _object(**fields):
    return func.jsonb_build_object(*(value for item in fields.items() for value in item))


def _outcome(record):
    return case((func.jsonb_typeof(record) == 'object',
                 _object(outcome=record['outcome'], accepted=record['accepted'], observed_at=record['observed_at'])))


def _summary_page(query):
    """Convert each paginated document once, then read the fields used by both summary APIs."""
    page = query.subquery('incident_page')
    # Keep the conversion in its own query so each JSON path does not parse the entire document.
    parsed = select(cast(page.c.document, JSONB).label('value')).correlate(page).offset(0).lateral('parsed_document')
    document = parsed.c.value
    data = document['data']
    release = data['release']
    projection = _object(
        id=func.coalesce(document['id'], func.to_jsonb(page.c.id)),
        state=func.coalesce(document['state'], func.to_jsonb(page.c.state)),
        created_at=func.coalesce(document['created_at'], func.to_jsonb(page.c.created_at)),
        environment=document['environment'],
        service=document['service'],
        system=document['system'],
        reason=func.coalesce(document['reason'], cast(literal('""'), JSONB)),
        data=_object(
            unified_origin=data['unified_origin'],
            display_readings=_object(**{'signal/reason': _object(ko=data[('display_readings', 'signal/reason', 'ko')])}),
            followups=data['followups'],
            round=func.coalesce(data['round'], cast(literal('1'), JSONB)),
            release=_object(
                protocol=release['protocol'],
                approval=_object(consumed_by_job_id=release[('approval', 'consumed_by_job_id')]),
                qa=_outcome(release['qa']),
                recovery=_outcome(release['recovery']),
            ),
        ),
    )
    return (select(projection).select_from(page.join(parsed, true()))
            .order_by(page.c.created_at.desc(), page.c.id.desc()))


def read_summary_page(session, query):
    try:
        return list(session.scalars(_summary_page(query)))
    except DataError as exc:
        # The JSON column can hold legacy values that JSONB cannot represent.
        if getattr(exc.orig, 'sqlstate', None) not in {'22P05', '22003'}:
            raise
        session.rollback()
        return None
