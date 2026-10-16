def test_state_machine_basic():
    from app.core.state_machine import CallSession
    s = CallSession('test')
    # initial prompt should ask about Medicare
    p1 = s.get_next_bot_prompt()
    assert 'Medicare' in p1
    # user says yes
    r1, t1 = s.process_user_utterance('yes')
    assert 'Are you between' in r1
    # user provides age
    r2, t2 = s.process_user_utterance('67')
    assert t2 is True
