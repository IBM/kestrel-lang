import json
import pytest
import os
from kestrel import Session
from pandas import DataFrame
from uuid import uuid4

from kestrel.display import GraphExplanation
from kestrel.ir.instructions import Construct
from kestrel.config.internal import CACHE_INTERFACE_IDENTIFIER
from kestrel.cache import SqliteCache


def test_execute_in_cache():
    hf = """
proclist = NEW process [ {"name": "cmd.exe", "pid": 123}
                       , {"name": "explorer.exe", "pid": 99}
                       , {"name": "firefox.exe", "pid": 201}
                       , {"name": "chrome.exe", "pid": 205}
                       ]
browsers = proclist WHERE name != "cmd.exe"
DISP browsers
cmd = proclist WHERE name = "cmd.exe"
DISP cmd ATTR pid
"""
    b1 = DataFrame([ {"name": "explorer.exe", "pid": 99}
                   , {"name": "firefox.exe", "pid": 201}
                   , {"name": "chrome.exe", "pid": 205}
                   ])
    b2 = DataFrame([ {"pid": 123} ])
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert b1.equals(next(res))
        assert b2.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


def test_execute_in_cache_stix_process():
    hf = """
proclist = NEW process [ {"binary_ref.name": "cmd.exe", "pid": 123}
                       , {"binary_ref.name": "explorer.exe", "pid": 99}
                       , {"binary_ref.name": "firefox.exe", "pid": 201}
                       , {"binary_ref.name": "chrome.exe", "pid": 205}
                       ]
DISP proclist ATTR binary_ref.name
"""
    b1 = DataFrame([ {"binary_ref.name": "cmd.exe"}
                   , {"binary_ref.name": "explorer.exe"}
                   , {"binary_ref.name": "firefox.exe"}
                   , {"binary_ref.name": "chrome.exe"}
                   ])
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert b1.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


@pytest.mark.skip("TODO: need attr mapping for Construct")
def test_execute_in_cache_stix_process_ocsf_disp_attr():
    hf = """
proclist = NEW process [ {"binary_ref.name": "cmd.exe", "pid": 123}
                       , {"binary_ref.name": "explorer.exe", "pid": 99}
                       , {"binary_ref.name": "firefox.exe", "pid": 201}
                       , {"binary_ref.name": "chrome.exe", "pid": 205}
                       ]
DISP proclist ATTR file.name
"""
    b1 = DataFrame([ {"file.name": "cmd.exe"}
                   , {"file.name": "explorer.exe"}
                   , {"file.name": "firefox.exe"}
                   , {"file.name": "chrome.exe"}
                   ])
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert b1.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


def test_execute_in_cache_stix_process_filtered():
    hf = """
proclist = NEW process [ {"binary_ref.name": "cmd.exe", "pid": 123}
                       , {"binary_ref.name": "explorer.exe", "pid": 99}
                       , {"binary_ref.name": "firefox.exe", "pid": 201}
                       , {"binary_ref.name": "chrome.exe", "pid": 205}
                       ]
browsers = proclist WHERE binary_ref.name in ('chrome.exe', 'firefox.exe')
DISP browsers ATTR binary_ref.name, pid
"""
    b1 = DataFrame([ {"binary_ref.name": "firefox.exe", "pid": 201}
                   , {"binary_ref.name": "chrome.exe", "pid": 205}
                   ])
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert b1.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


def test_execute_in_cache_stix_process_with_ref_and_multi_returns():
    hf = """
proclist = NEW process [ {"binary_ref.name": "cmd.exe", "pid": 123}
                       , {"binary_ref.name": "explorer.exe", "pid": 99}
                       , {"binary_ref.name": "firefox.exe", "pid": 201}
                       , {"binary_ref.name": "chrome.exe", "pid": 205}
                       ]
newvar = proclist WHERE binary_ref.name = "cmd.exe"
DISP proclist ATTR binary_ref.name
newvar2 = proclist WHERE binary_ref.name IN ("explorer.exe", "cmd.exe")
newvar3 = newvar2 WHERE pid IN newvar.pid
DISP newvar3 ATTR binary_ref.name
"""
    b1 = DataFrame([ {"binary_ref.name": "cmd.exe"}
                   , {"binary_ref.name": "explorer.exe"}
                   , {"binary_ref.name": "firefox.exe"}
                   , {"binary_ref.name": "chrome.exe"}
                   ])
    b2 = DataFrame([ {"binary_ref.name": "cmd.exe"}
                   ])
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert b1.equals(next(res))
        assert b2.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


def test_execute_in_cache_stix_file():
    data = [ {"name": "cmd.exe", "hashes.MD5": "AD7B9C14083B52BC532FBA5948342B98"}
           , {"name": "powershell.exe", "hashes.MD5": "04029E121A0CFA5991749937DD22A1D9"}
    ]
    hf = f"""
filelist = NEW file {json.dumps(data)}
DISP filelist ATTR name, hashes.MD5
"""
    b1 = DataFrame(data)
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert b1.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


def test_double_deref_in_cache():
    # When the Filter node is dereferred twice
    # The node should be deepcopied each time to avoid issue
    hf = """
proclist = NEW process [ {"name": "cmd.exe", "pid": 123}
                       , {"name": "explorer.exe", "pid": 99}
                       , {"name": "firefox.exe", "pid": 201}
                       , {"name": "chrome.exe", "pid": 205}
                       ]
px = proclist WHERE name != "cmd.exe" AND pid = 205
chrome = proclist WHERE pid IN px.pid
DISP chrome
DISP chrome
"""
    df = DataFrame([ {"name": "chrome.exe", "pid": 205} ])
    with Session() as session:
        res = session.execute_to_generate(hf)
        assert df.equals(next(res))
        assert df.equals(next(res))
        with pytest.raises(StopIteration):
            next(res)


def test_explain_in_cache():
    hf = """
proclist = NEW process [ {"name": "cmd.exe", "pid": 123}
                       , {"name": "explorer.exe", "pid": 99}
                       , {"name": "firefox.exe", "pid": 201}
                       , {"name": "chrome.exe", "pid": 205}
                       ]
browsers = proclist WHERE name != "cmd.exe"
chrome = browsers WHERE pid = 205
EXPLAIN chrome
"""
    with Session() as session:
        ress = session.execute_to_generate(hf)
        res = next(ress)
        assert isinstance(res, GraphExplanation)
        assert len(res.graphlets) == 1
        ge = res.graphlets[0]
        assert ge.graph == session.irgraph.to_dict()
        construct = session.irgraph.get_nodes_by_type(Construct)[0]
        assert ge.query.language == "SQL"
        stmt = ge.query.statement.replace('"', '')
        assert stmt == f"WITH proclist AS \n(SELECT * \nFROM {construct.id.hex}v), \nbrowsers AS \n(SELECT * \nFROM proclist \nWHERE name != 'cmd.exe'), \nchrome AS \n(SELECT * \nFROM browsers \nWHERE pid = 205)\n SELECT * \nFROM chrome"
        with pytest.raises(StopIteration):
            next(ress)


def test_multi_interface_explain():

    class DataLake(SqliteCache):
        @staticmethod
        def schemes():
            return ["datalake"]

    class Gateway(SqliteCache):
        @staticmethod
        def schemes():
            return ["gateway"]

    extra_db = []
    with Session() as session:
        stmt1 = """
procs = NEW process [ {"name": "cmd.exe", "pid": 123}
                    , {"name": "explorer.exe", "pid": 99}
                    , {"name": "firefox.exe", "pid": 201}
                    , {"name": "chrome.exe", "pid": 205}
                    ]
DISP procs
"""
        session.execute(stmt1)
        session.interface_manager[CACHE_INTERFACE_IDENTIFIER].__class__ = DataLake
        session.irgraph.get_nodes_by_type_and_attributes(Construct, {"interface": CACHE_INTERFACE_IDENTIFIER})[0].interface = "datalake"

        new_cache = SqliteCache(session_id = uuid4())
        extra_db.append(new_cache.db_path)
        session.interface_manager.interfaces.append(new_cache)
        stmt2 = """
nt = NEW network [ {"pid": 123, "source": "192.168.1.1", "destination": "1.1.1.1"}
                 , {"pid": 205, "source": "192.168.1.1", "destination": "1.1.1.2"}
                 ]
DISP nt
"""
        session.execute(stmt2)
        session.interface_manager[CACHE_INTERFACE_IDENTIFIER].__class__ = Gateway
        session.irgraph.get_nodes_by_type_and_attributes(Construct, {"interface": CACHE_INTERFACE_IDENTIFIER})[0].interface = "gateway"

        new_cache = SqliteCache(session_id = uuid4())
        extra_db.append(new_cache.db_path)
        session.interface_manager.interfaces.append(new_cache)
        stmt3 = """
domain = NEW domain [ {"ip": "1.1.1.1", "domain": "cloudflare.com"}
                    , {"ip": "1.1.1.2", "domain": "xyz.cloudflare.com"}
                    ]
DISP domain
"""
        session.execute(stmt3)

        stmt = """
p2 = procs WHERE name IN ("firefox.exe", "chrome.exe")
ntx = nt WHERE pid IN p2.pid
d2 = domain WHERE ip IN ntx.destination
EXPLAIN d2
DISP d2
"""
        ress = session.execute_to_generate(stmt)
        disp = next(ress)
        df_res = next(ress)

        with pytest.raises(StopIteration):
            next(ress)

        assert isinstance(disp, GraphExplanation)
        assert len(disp.graphlets) == 4

        assert len(disp.graphlets[0].graph["nodes"]) == 5
        query = disp.graphlets[0].query.statement.replace('"', '')
        procs = session.irgraph.get_variable("procs")
        c1 = next(session.irgraph.predecessors(procs))
        assert query == f"WITH procs AS \n(SELECT * \nFROM {c1.id.hex}), \np2 AS \n(SELECT * \nFROM procs \nWHERE name IN ('firefox.exe', 'chrome.exe'))\n SELECT pid \nFROM p2"

        assert len(disp.graphlets[1].graph["nodes"]) == 2
        query = disp.graphlets[1].query.statement.replace('"', '')
        nt = session.irgraph.get_variable("nt")
        c2 = next(session.irgraph.predecessors(nt))
        assert query == f"WITH nt AS \n(SELECT * \nFROM {c2.id.hex})\n SELECT * \nFROM nt"

        # the current session.execute_to_generate() logic does not store
        # in cache if evaluated by cache; the behavior may change in the future
        assert len(disp.graphlets[2].graph["nodes"]) == 2
        query = disp.graphlets[2].query.statement.replace('"', '')
        domain = session.irgraph.get_variable("domain")
        c3 = next(session.irgraph.predecessors(domain))
        assert query == f"WITH domain AS \n(SELECT * \nFROM {c3.id.hex})\n SELECT * \nFROM domain"

        assert len(disp.graphlets[3].graph["nodes"]) == 12
        query = disp.graphlets[3].query.statement.replace('"', '')
        p2 = session.irgraph.get_variable("p2")
        p2pa = next(session.irgraph.successors(p2))
        assert query == f"WITH domain AS \n(SELECT * \nFROM {c3.id.hex}), \nntx AS \n(SELECT * \nFROM {nt.id.hex}v \nWHERE pid IN (SELECT * \nFROM {p2pa.id.hex}v)), \nd2 AS \n(SELECT * \nFROM domain \nWHERE ip IN (SELECT destination \nFROM ntx))\n SELECT * \nFROM d2"

        df_ref = DataFrame([{"ip": "1.1.1.2", "domain": "xyz.cloudflare.com"}])
        assert df_ref.equals(df_res)

    for db_file in extra_db:
        os.remove(db_file)
