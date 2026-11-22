"""Shared, non-bypassable speaker/voice gates for private-listen jobs."""
from diarization_cache import VOICE_ORDER


class SpeakerGateError(ValueError):
    code = 'speaker_gate_failed'


def use_male_slots(config):
    """Default 1–3 person shows to male slots so an ad read cannot invent a female voice.

    Four or five real speakers still use the full pool: there are only three
    male slots, and the voice gate requires one distinct slot each. Set
    ``prefer_male_slots`` false when a two-hander actually has a woman.
    """
    expected = expected_speakers(config)
    if expected > 3:
        return False
    if 'prefer_male_slots' in config:
        return config.get('prefer_male_slots') is True
    return True


def requires_voice_mapping(config):
    return config.get('prefer_male_slots') is False and config.get('male_only') is not True


def expected_speakers(config):
    # Legacy jobs get the same safe defaults; never infer N from a bad document.
    count = config.get('expected_speakers', config.get('speaker_count'))
    if count is None:
        count = 1 if config.get('content_type') in ('monologue', 'book') else 2
    if type(count) is not int or not 1 <= count <= len(VOICE_ORDER):
        raise SpeakerGateError(f'声明人数须为 1 至 {len(VOICE_ORDER)} 的整数')
    if config.get('content_type') == 'book' and count != 1:
        raise SpeakerGateError('书听只允许 1 位朗读者')
    return count


def validate_speaker_ids(config, speaker_ids):
    expected = expected_speakers(config)
    ids = set(speaker_ids)
    if None in ids or '' in ids or len(ids) != expected:
        raise SpeakerGateError(
            f'人数硬闸：声明 {expected} 人，实际 {len(ids)} 人。已禁止合成；'
            '请核对声明人数，重新准备译稿或修正说话人分配后重试。跨说话人的段落请拆段并逐段确认。')
    return ids


def validate_speaker_voices(config, document, *, confirming=False):
    # Check the whole document before preview selection, including silent cues.
    ids = validate_speaker_ids(config, (s.get('speaker_id') for s in document['segments']))
    records = document.get('speakers', {})
    slots = [records.get(speaker, {}).get('voice_slot') for speaker in ids]
    if any(slot not in VOICE_ORDER for slot in slots) or len(set(slots)) != expected_speakers(config):
        raise SpeakerGateError(
            f'音色硬闸：须为 {expected_speakers(config)} 人各绑定一个不同的有效音色。'
            '请逐人设置声槽，再用 confirm-voices 确认，禁止多人共用声槽。')
    if requires_voice_mapping(config) and not confirming and (
        document.get('voices_confirmed') is not True
        or document.get('confirmed_voice_slots') != dict(zip(ids, slots))
    ):
        raise SpeakerGateError('请逐人核对声槽，并用 confirm-voices 确认后再合成。')
