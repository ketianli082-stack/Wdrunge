"""Run model calls through real LangGraph injection/dispatch, not just Processes."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode

from app.agents.tools import development
from app.agents.tools.validation import tool_error


def graph():
    builder=StateGraph(MessagesState)
    builder.add_node('tools',ToolNode(list(development.development_tools),handle_tool_errors=tool_error))
    builder.add_edge(START,'tools');builder.add_edge('tools',END)
    return builder.compile()


@pytest.mark.parametrize('arguments,expected,background',[
    ({'argv': ['python3', '--version']},['python3','--version'],False),
    ({'argv': ['pwd']},['pwd'],False),
    ({'argv': ['npm', 'run','dev'],'background':True,'name':'frontend','timeout':1800},['npm','run','dev'],True),
    ({'argv': ['npm', 'ci'],'cwd':'frontend','network':True},['npm','ci'],False),
    ({'argv': ['python3', '-m','venv','.venv']},['python3','-m','venv','.venv'],False),
])
def test_model_call_reaches_execution_policy(monkeypatch,arguments,expected,background):
    calls=[]
    async def run(argv,*args,**kwargs):
        calls.append((argv,False,args,kwargs));return {'exit_code':0,'output':'checked'}
    async def start(argv,*args,**kwargs):
        calls.append((argv,True,args,kwargs))
        return SimpleNamespace(snapshot=lambda:{'id':'service'},watcher=asyncio.get_running_loop().create_future(),
                               process=SimpleNamespace(returncode=None),timed_out=False,ports=set())
    monkeypatch.setattr(development.processes,'run',run)
    monkeypatch.setattr(development.processes,'start',start)
    monkeypatch.setattr(development.processes,'logs',lambda job,offset=0,limit=0:{'output':''})
    monkeypatch.setattr(development,'STARTUP_GRACE',.01)
    async def scenario():
        result=await graph().ainvoke({'messages':[AIMessage(content='',tool_calls=[{'id':'call','name':'execute_command','args':arguments}])]})
        reply=result['messages'][-1]
        assert reply.status=='success',reply.content
        assert 'error' not in json.loads(reply.content)
        assert len(calls)==1 and calls[0][:2]==(expected,background)
        assert calls[0][2][0]==arguments.get('cwd','.')
        if arguments.get('network'):assert calls[0][3]['network'] is True
    asyncio.run(scenario())


def test_public_schemas_do_not_leak_reserved_parameters():
    assert {t.name for t in development.development_tools}=={'execute_command','manage_process','detect_environment'}
    for candidate in development.development_tools:
        schema=convert_to_openai_tool(candidate)['function']['parameters']
        assert not {'args','kwargs','v__args','v__kwargs','runtime'} & schema['properties'].keys()
    schema=convert_to_openai_tool(development.execute_command)['function']['parameters']
    assert schema['required']==['argv'] and 'command' not in schema['properties']
    assert schema['properties']['argv']['items']['type']=='string' and schema['properties']['argv']['minItems']==1
    with pytest.raises(ValueError):development.execute_command.args_schema.model_validate({'argv':'echo hi'})
    with pytest.raises(ValueError):development.execute_command.args_schema.model_validate({'argv':[]})


def test_foreground_timeout_guard(monkeypatch):
    async def unexpected(*args,**kwargs):raise AssertionError('Must reject before execution')
    monkeypatch.setattr(development.processes,'run',unexpected)
    async def run():
        result=await graph().ainvoke({'messages':[AIMessage(content='',tool_calls=[{'id':'call','name':'execute_command','args':{'argv': ['echo'],'timeout':1800}}])]})
        assert result['messages'][-1].status=='error'
        assert 'Foreground timeout' in result['messages'][-1].content
    asyncio.run(run())
