from samba import Ldb
from samba.auth import system_session
from samba import ldb
import traceback
from yast import ycpbuiltins, import_module
import_module('UI')
from yast import UI
from samba.credentials import MUST_USE_KERBEROS
from adcommon.creds import kinit_for_gssapi, krb5_temp_conf, pdc_dns_name
from adcommon.strings import strcmp
from samba.net import Net
import os
import six
import ldapurl
import binascii, struct, re
from datetime import datetime
SCOPE_BASE = ldb.SCOPE_BASE
SCOPE_ONELEVEL = ldb.SCOPE_ONELEVEL
SCOPE_SUBTREE = ldb.SCOPE_SUBTREE

def addlist(attrs):
    return attrs

def modlist(old_attrs, new_attrs):
    for key in old_attrs:
        if key in new_attrs and old_attrs[key] == new_attrs[key]:
            del new_attrs[key]
    return new_attrs

def y2error_dialog(msg):
    from yast import UI, Opt, HBox, HSpacing, VBox, VSpacing, Label, Right, PushButton, Id
    if six.PY3 and type(msg) is bytes:
        msg = msg.decode('utf-8')
    ans = False
    UI.SetApplicationTitle('Error')
    UI.OpenDialog(Opt('warncolor'), HBox(HSpacing(1), VBox(
        VSpacing(.3),
        Label(msg),
        Right(HBox(
            PushButton(Id('ok'), 'OK')
        )),
        VSpacing(.3),
    ), HSpacing(1)))
    ret = UI.UserInput()
    if str(ret) == 'ok' or str(ret) == 'abort' or str(ret) == 'cancel':
        UI.CloseDialog()

class LdapException(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)
        if len(self.args) > 0:
            self.msg = self.args[0]
        else:
            self.msg = None
        if len(self.args) > 1:
            self.info = self.args[1]
        else:
            self.info = None

def stringify_ldap(data):
    if type(data) == dict:
        for key, value in data.items():
            data[key] = stringify_ldap(value)
        return data
    elif type(data) == list:
        new_list = []
        for item in data:
            new_list.append(stringify_ldap(item))
        return new_list
    elif type(data) == tuple:
        new_tuple = []
        for item in data:
            new_tuple.append(stringify_ldap(item))
        return tuple(new_tuple)
    elif six.PY2 and type(data) == unicode:
        return str(data)
    elif six.PY3 and isinstance(data, six.string_types):
        return data.encode('utf-8') # python3-ldap requires a bytes type
    else:
        return data

class Ldap(Ldb):
    def __init__(self, lp, creds, ldap_url=None):
        self.lp = lp
        self.creds = creds
        self.realm = lp.get('realm')
        self.realm_dn = ','.join(['DC=%s' % part for part in self.realm.lower().split('.')])
        self.ldap_url = ldapurl.LDAPUrl(ldap_url) if ldap_url else None
        self.__ldap_connect()
        # Ugly Hack: Make the ldap module backwards compatible with modules that
        # called functions of l (the python ldap code).
        self.l = self
        self.net = Net(creds=self.creds, lp=self.lp)
        self.schema = {}
        self.__load_schema()

    def __ldap_exc_msg(self, e):
        if len(e.args) > 0 and \
          type(e.args[-1]) is dict and \
          'desc' in e.args[-1]:
            return e.args[-1]['desc']
        else:
            return str(e)

    def __ldap_exc_info(self, e):
        if len(e.args) > 0 and \
          type(e.args[-1]) is dict and \
          'info' in e.args[-1]:
            return e.args[-1]['info']
        else:
            return ''

    def __ldap_connect(self):
        self.dc_hostname = pdc_dns_name(self.realm)
        if not self.ldap_url:
            self.ldap_url = ldapurl.LDAPUrl('ldap://%s' % self.dc_hostname)
        if self.creds.get_kerberos_state() != MUST_USE_KERBEROS:
            kinit_for_gssapi(self.creds, self.realm)
        try:
            super(Ldap, self).__init__(url=self.ldap_url.initializeUrl(), lp=self.lp,
                                       credentials=self.creds, session_info=system_session())
        except ldb.LdbError:
            ycpbuiltins.y2error('Failed to initialize ldap connection')
            raise Exception('Failed to initialize ldap connection')

    def __ldap_disconnect(self, e):
        if e.args[0] == ldb.ERR_OPERATIONS_ERROR and \
          ('connection to remote LDAP server dropped' in e.args[1] or 'NT_STATUS_CONNECTION_RESET' in e.args[1]):
            return True
        return False

    def ldap_search_s(self, *args):
        return self.ldap_search(*args)

    def ldap_search(self, base=None, scope=None, expression=None, attrs=None, controls=None):
        try:
            attrs = [a.decode() if type(a) is six.binary_type else a for a in attrs]
            try:
                return [(str(m.get('dn')), {k: [bytes(v) for v in m.get(k)] for k in m.keys() if k != 'dn'}) for m in self.search(base, scope, expression, attrs, controls)]
            except ldb.LdbError as e:
                if self.__ldap_disconnect(e):
                    self.__ldap_connect()
                    return [(str(m.get('dn')), {k: [bytes(v) for v in m.get(k)] for k in m.keys() if k != 'dn'}) for m in self.search(base, scope, expression, attrs, controls)]
                else:
                    raise
        except ldb.LdbError as e:
            y2error_dialog(self.__ldap_exc_msg(e))
        except Exception as e:
            ycpbuiltins.y2error(traceback.format_exc())
            ycpbuiltins.y2error('ldap.search_s: %s\n' % self.__ldap_exc_msg(e))

    def ldap_add(self, dn, attrs):
        try:
            attrs['dn'] = dn
            try:
                return self.add(attrs)
            except ldb.LdbError as e:
                if self.__ldap_disconnect(e):
                    self.__ldap_connect()
                    return self.add(attrs)
                else:
                    raise
        except Exception as e:
            raise LdapException(self.__ldap_exc_msg(e), self.__ldap_exc_info(e))

    def ldap_modify(self, dn, attrs):
        # Check to see if it's an ldap message instead of key/value pairs
        if type(attrs) != dict:
            # Convert the ldap message into key/value pair strings, ignoring old values
            attrs = {m[1].decode() if type(m[1]) is six.binary_type else m[1]: m[2].decode() if type(m[2]) is six.binary_type else str(m[2]) for m in attrs if m[0] == 0}
        try:
            attrs['dn'] = dn
            try:
                return self.modify(ldb.Message.from_dict(self, attrs))
            except ldb.LdbError as e:
                if self.__ldap_disconnect(e):
                    self.__ldap_connect()
                    return self.modify(ldb.Message.from_dict(self, attrs))
                else:
                    raise
        except ldb.LdbError as e:
            y2error_dialog(self.__ldap_exc_msg(e))
        except Exception as e:
            ycpbuiltins.y2error(traceback.format_exc())
            ycpbuiltins.y2error('ldap.modify: %s\n' % self.__ldap_exc_msg(e))

    def ldap_delete(self, *args):
        try:
            try:
                return self.delete(*args)
            except ldb.LdbError as e:
                if self.__ldap_disconnect(e):
                    self.__ldap_connect()
                    return self.delete(*args)
                else:
                    raise
        except ldb.LdbError as e:
            y2error_dialog(self.__ldap_exc_msg(e))
        except Exception as e:
            ycpbuiltins.y2error(traceback.format_exc())
            ycpbuiltins.y2error('ldap.delete_s: %s\n' % self.__ldap_exc_msg(e))

    def rename_s(self, dn, newrdn, newsuperior):
        try:
            try:
                super(Ldap, self).rename(dn, '%s,%s' % (newrdn, newsuperior))
            except ldb.LdbError as e:
                if self.__ldap_disconnect(e):
                    self.__ldap_connect()
                    super(Ldap, self).rename(dn, '%s,%s' % (newrdn, newsuperior))
                else:
                    raise
        except ldb.LdbError as e:
            y2error_dialog(self.__ldap_exc_msg(e))
        except Exception as e:
            ycpbuiltins.y2error(traceback.format_exc())
            ycpbuiltins.y2error('ldap.rename: %s\n' % self.__ldap_exc_msg(e))

    def __find_inferior_classes(self, name):
        dn = self.get_schema_basedn()
        search = '(|(possSuperiors=%s)(systemPossSuperiors=%s))' % (name, name)
        return [item[-1]['lDAPDisplayName'][-1] for item in self.ldap_search_s(dn, SCOPE_SUBTREE, search, ['lDAPDisplayName'])]

    def schema_request_inferior_classes(self, name):
        if not self.schema['objectClasses'][name]['inferior']:
            self.schema['objectClasses'][name]['inferior'] = self.__find_inferior_classes(name.decode())
        return self.schema['objectClasses'][name]['inferior']

    def __load_schema(self):
        dn = str(self.search('', SCOPE_BASE, '(objectclass=*)', ['subschemaSubentry'])[0]['subschemaSubentry'])
        results = self.search(dn, SCOPE_BASE, '(objectclass=*)', ['attributeTypes', 'dITStructureRules', 'objectClasses', 'nameForms', 'dITContentRules', 'matchingRules', 'ldapSyntaxes', 'matchingRuleUse'])[0]

        self.schema['attributeTypes'] = {}
        self.schema['constructedAttributes'] = self.__constructed_attributes()
        for attributeType in results['attributeTypes']:
            m = re.match(b'\(\s+(?P<id>[0-9\.]+)\s+NAME\s+\'(?P<name>[\-\w]+)\'\s+(SYNTAX\s+\'(?P<syntax>[0-9\.]+)\'\s+)?(?P<info>.*)\)', attributeType)
            if m:
                name = m.group('name')
                self.schema['attributeTypes'][name] = {}
                self.schema['attributeTypes'][name]['id'] = m.group('id')
                self.schema['attributeTypes'][name]['syntax'] = m.group('syntax')
                self.schema['attributeTypes'][name]['multi-valued'] = b'SINGLE-VALUE' not in m.group('info')
                self.schema['attributeTypes'][name]['collective'] = b'COLLECTIVE' in m.group('info')
                self.schema['attributeTypes'][name]['user-modifiable'] = b'NO-USER-MODIFICATION' not in m.group('info')
                if b'USAGE' in m.group('info'):
                    usage = re.findall(b'.*\s+USAGE\s+(\w+)', m.group('info'))
                    self.schema['attributeTypes'][name]['usage'] = usage[-1] if usage else b'userApplications'
                else:
                    self.schema['attributeTypes'][name]['usage'] = b'userApplications'
            else:
                raise ldap.LDAPError('Failed to parse attributeType: %s' % attributeType.decode())

        self.schema['objectClasses'] = {}
        for objectClass in results['objectClasses']:
            m = re.match(b'\(\s+(?P<id>[0-9\.]+)\s+NAME\s+\'(?P<name>[\-\w]+)\'\s+(SUP\s+(?P<superior>[\-\w]+)\s+)?(?P<type>\w+)\s+(MUST\s+\((?P<must>[^\)]*)\)\s+)?(MAY\s+\((?P<may>[^\)]*)\)\s+)?\)', objectClass)
            if m:
                name = m.group('name')
                self.schema['objectClasses'][name] = {}
                self.schema['objectClasses'][name]['id'] = m.group('id')
                self.schema['objectClasses'][name]['superior'] = m.group('superior')
                # Inferior classes should be loaded on-demand,
                # otherwise this causes significant startup delays
                self.schema['objectClasses'][name]['inferior'] = None
                self.schema['objectClasses'][name]['type'] = m.group('type')
                self.schema['objectClasses'][name]['must'] = m.group('must').strip().split(b' $ ') if m.group('must') else []
                self.schema['objectClasses'][name]['may'] = m.group('may').strip().split(b' $ ') if m.group('may') else []
            else:
                raise ldap.LDAPError('Failed to parse objectClass: %s' % objectClass.decode())

        self.schema['dITContentRules'] = {}
        for dITContentRule in results['dITContentRules']:
            m = re.match(b'\(\s+(?P<id>[0-9\.]+)\s+NAME\s+\'(?P<name>[\-\w]+)\'\s*(AUX\s+\((?P<aux>[^\)]*)\))?\s*(MUST\s+\((?P<must>[^\)]*)\)\s+)?\s*(MAY\s+\((?P<may>[^\)]*)\))?\s*(NOT\s+\((?P<not>[^\)]*)\))?\s*\)', dITContentRule)
            if m:
                name = m.group('name')
                self.schema['dITContentRules'][name] = {}
                self.schema['dITContentRules'][name]['id'] = m.group('id')
                self.schema['dITContentRules'][name]['must'] = m.group('must').strip().split(b' $ ') if m.group('must') else []
                self.schema['dITContentRules'][name]['may'] = m.group('may').strip().split(b' $ ') if m.group('may') else []
                self.schema['dITContentRules'][name]['aux'] = m.group('aux').strip().split(b' $ ') if m.group('aux') else []
                self.schema['dITContentRules'][name]['not'] = m.group('not').strip().split(b' $ ') if m.group('not') else []
            else:
                raise ldap.LDAPError('Failed to parse dITContentRule: %s' % dITContentRule.decode())

    def __constructed_attributes(self):
        # ADSI Hides constructed attributes, since they can't be modified.
        # 1.2.840.113556.1.4.803 is the OID for LDAP_MATCHING_RULE_BIT_AND (we're and'ing 4 on systemFlags)
        search = '(&(systemFlags:1.2.840.113556.1.4.803:=4)(ObjectClass=attributeSchema))'
        container = self.get_schema_basedn()
        ret = self.ldap_search(container, SCOPE_ONELEVEL, search, ['lDAPDisplayName'])
        return [a[-1]['lDAPDisplayName'][-1] for a in ret]

    def __timestamp(self, val):
        return str(datetime.strptime(val.decode(), '%Y%m%d%H%M%S.%fZ'))

    def __display_value_each(self, syntax, key, val):
        # rfc4517 Generalized Time syntax
        if syntax == b'1.3.6.1.4.1.1466.115.121.1.24':
            return self.__timestamp(val)
        # rfc4517 Octet String syntax
        if syntax == b'1.3.6.1.4.1.1466.115.121.1.40':
            if key == 'objectGUID':
                return octet_string_to_objectGUID(val)
            elif key == 'objectSid':
                return octet_string_to_objectSid(val)
            else:
                return octet_string_to_hex(val)
        return val

    def display_schema_value(self, key, val):
        if key.encode() in self.schema['attributeTypes']:
            attr_type = self.schema['attributeTypes'][key.encode()]
        else:
            # RootDSE attributes don't show up in the schema, so we have to guess
            if len(val) > 1: # multi-valued
                return '; '.join([v.decode() for v in val])
            return val[-1]
        if val == None:
            return '<not set>'
        else:
            if not attr_type['multi-valued']:
                return self.__display_value_each(attr_type['syntax'], key, val[-1])
            ret = []
            for sval in val:
                nval = self.__display_value_each(attr_type['syntax'], key, sval)
                if isinstance(nval, six.binary_type):
                    nval = nval.decode()
                ret.append(nval)
            return '; '.join(ret)

def octet_string_to_hex(data):
    return binascii.hexlify(data)

def octet_string_to_objectGUID(data):
    return '%s-%s-%s-%s-%s' % ('%02x' % struct.unpack('<L', data[0:4])[0],
                               '%02x' % struct.unpack('<H', data[4:6])[0],
                               '%02x' % struct.unpack('<H', data[6:8])[0],
                               '%02x' % struct.unpack('>H', data[8:10])[0],
                               '%02x%02x' % struct.unpack('>HL', data[10:]))

def octet_string_to_objectSid(data):
    if struct.unpack('B', chr(data[0]).encode())[0] == 1:
        length = struct.unpack('B', chr(data[1]).encode())[0]-1
        security_nt_authority = struct.unpack('>xxL', data[2:8])[0]
        security_nt_non_unique = struct.unpack('<L', data[8:12])[0]
        ret = 'S-1-%d-%d' % (security_nt_authority, security_nt_non_unique)
        for i in range(length):
            pos = 12+(i*4)
            ret += '-%d' % struct.unpack('<L', data[pos:pos+4])
        return ret
    else:
        return octet_string_to_hex(data)
