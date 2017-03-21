import sys
import os
import collections
import logging
import subprocess
import clang.cindex
from services.parser.ast_node_identifier import ASTNodeId
from pympler import asizeof

class ChildVisitResult(clang.cindex.BaseEnumeration):
    """
    A ChildVisitResult describes how the traversal of the children of a particular cursor should proceed after visiting a particular child cursor.
    """
    _kinds = []
    _name_map = None

    def __repr__(self):
        return 'ChildVisitResult.%s' % (self.name,)

ChildVisitResult.BREAK = ChildVisitResult(0) # Terminates the cursor traversal.
ChildVisitResult.CONTINUE = ChildVisitResult(1) # Continues the cursor traversal with the next sibling of the cursor just visited, without visiting its children.
ChildVisitResult.RECURSE = ChildVisitResult(2) # Recursively traverse the children of this cursor, using the same visitor and client data.

def default_visitor(child, parent, client_data):
    """Default implementation of AST node visitor."""

    return ChildVisitResult.CONTINUE.value

def traverse(cursor, client_data, client_visitor = default_visitor):
    """Traverse AST using the client provided visitor."""

    def visitor(child, parent, client_data):
        assert child != clang.cindex.conf.lib.clang_getNullCursor()
        child._tu = cursor._tu
        child.ast_parent = parent
        return client_visitor(child, parent, client_data)

    return clang.cindex.conf.lib.clang_visitChildren(cursor, clang.cindex.callbacks['cursor_visit'](visitor), client_data)

def get_children_patched(self, traversal_type = ChildVisitResult.CONTINUE):
    """
    Return an iterator for accessing the children of this cursor.
    This is a patched version of Cursor.get_children() but which is built on top of new traversal interface.
    See traverse() for more details.
    """

    def visitor(child, parent, children):
        children.append(child)
        return traversal_type.value

    children = []
    traverse(self, children, visitor)
    return iter(children)

"""
Monkey-patch the existing Cursor.get_children() with get_children_patched().
This is a temporary solution and should be removed once, and if, it becomes available in official libclang Python bindings.
New version provides more functionality (i.e. AST parent node) which is needed in certain cases.
"""
clang.cindex.Cursor.get_children = get_children_patched

class ImmutableSourceLocation():
    """
    Reason of existance of this class is because clang.cindex.SourceLocation is not designed to be hashable.
    """

    def __init__(self, source_location):
        self.source_location = source_location

    @property
    def file(self):
        """Get the file represented by this source location."""
        return self.source_location.file

    @property
    def line(self):
        """Get the line represented by this source location."""
        return self.source_location.line

    @property
    def column(self):
        """Get the column represented by this source location."""
        return self.source_location.column

    @property
    def offset(self):
        """Get the file offset represented by this source location."""
        return self.source_location.offset

    def __eq__(self, other):
        return self.source_location.__eq__(other.source_location)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.source_location.file.name) ^ hash(self.source_location.line) ^ hash(self.source_location.column) ^ hash(self.source_location.offset)

    def __repr__(self):
        if self.source_location.file:
            filename = self.source_location.file.name
        else:
            filename = None
        return "<ImmutableSourceLocation file %r, line %r, column %r>" % (
            filename, self.source_location.line, self.source_location.column)


def get_system_includes():
    output = subprocess.Popen(["g++", "-v", "-E", "-x", "c++", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
    pattern = ["#include <...> search starts here:", "End of search list."]
    output = str(output)
    return output[output.find(pattern[0]) + len(pattern[0]) : output.find(pattern[1])].replace(' ', '-I').split('\\n')

class ClangParser():
    def __init__(self):
        self.tunits = {}
        self.index = clang.cindex.Index.create()
        self.default_args = ['-x', 'c++', '-std=c++14'] + get_system_includes()

    def run(self, contents_filename, original_filename, compiler_args, project_root_directory):
        logging.info('Filename = {0}'.format(original_filename))
        logging.info('Contents Filename = {0}'.format(contents_filename))
        logging.info('Default args = {0}'.format(self.default_args))
        logging.info('User-provided compiler args = {0}'.format(compiler_args))
        logging.info('Compiler working-directory = {0}'.format(project_root_directory))
        try:
            # Parse the translation unit
            self.tunits[original_filename] = self.index.parse(
                path = contents_filename,
                args = self.default_args + compiler_args + ['-working-directory=' + project_root_directory],
                options = clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD # TODO CXTranslationUnit_KeepGoing?
            )
        except:
            logging.error(sys.exc_info()[0])

        logging.info('TUnits memory consumption (pympler) = ' + str(asizeof.asizeof(self.tunits)))
        logging.info("tunits: " + str(self.tunits))

    def get_translation_unit(self, filename):
        return self.tunits.get(filename, None)

    def get_diagnostics(self, filename):
        if filename in self.tunits:
            logging.info("get_diagnostics() for " + filename + " tunit: " + str(self.tunits[filename]))
            return self.tunits[filename].diagnostics
        return None

    def traverse(self, cursor, client_data, client_visitor):
        traverse(cursor, client_data, client_visitor)

    def get_ast_node_id(self, cursor):
        # We have to handle (at least) two different situations when libclang API will not give us enough details about the given cursor directly:
        #   1. When Cursor.TypeKind is DEPENDENT
        #       * Cursor.TypeKind happens to be set to DEPENDENT for constructs whose semantics may differ from one
        #         instantiation to another. These are called dependent names (see 14.6.2 [temp.dep] in C++ standard).
        #       * Example can be a call expression on non-instantiated function template, or even a reference to
        #         a data member of non-instantiated class template.
        #       * In this case we try to extract the right CursorKind by tokenizing the given cursor, selecting the
        #         right token and, depending on its position in the AST tree, return the right CursorKind information.
        #         See ClangParser.__extract_dependent_type_kind() for more details.
        #       * Similar actions have to be taken for extracting spelling and location for such cursors.
        #   2. When Cursor.Kind is OVERLOADED_DECL_REF
        #       * Cursor.Kind.OVERLOADED_DECL_REF basically identifies a reference to a set of overloaded functions
        #         or function templates which have not yet been resolved to a specific function or function template.
        #       * This means that token kind might be one of the following:
        #            Cursor.Kind.FUNCTION_DECL, Cursor.Kind.FUNCTION_TEMPLATE, Cursor.Kind.CXX_METHOD
        #       * To extract more information about the token we can use `clang_getNumOverloadedDecls()` to get how
        #         many overloads there are and then use `clang_getOverloadedDecl()` to get a specific overload.
        #       * In our case, we can always use the first overload which explains hard-coded 0 as an index.
        if cursor.type.kind == clang.cindex.TypeKind.DEPENDENT:
            return ClangParser.to_ast_node_id(ClangParser.__extract_dependent_type_kind(cursor))
        else:
            if cursor.referenced:
                if (cursor.referenced.kind == clang.cindex.CursorKind.OVERLOADED_DECL_REF):
                    if (ClangParser.__get_num_overloaded_decls(cursor.referenced)):
                        return ClangParser.to_ast_node_id(ClangParser.__get_overloaded_decl(cursor.referenced, 0).kind)
                return ClangParser.to_ast_node_id(cursor.referenced.kind)
            if (cursor.kind == clang.cindex.CursorKind.OVERLOADED_DECL_REF):
                if (ClangParser.__get_num_overloaded_decls(cursor)):
                    return ClangParser.to_ast_node_id(ClangParser.__get_overloaded_decl(cursor, 0).kind)
        return ClangParser.to_ast_node_id(cursor.kind)

    def get_ast_node_name(self, cursor):
        if cursor.type.kind == clang.cindex.TypeKind.DEPENDENT:
            return ClangParser.__extract_dependent_type_spelling(cursor)
        else:
            if (cursor.referenced):
                return cursor.referenced.spelling
            else:
                return cursor.spelling

    def get_ast_node_line(self, cursor):
        if cursor.type.kind == clang.cindex.TypeKind.DEPENDENT:
            return ClangParser.__extract_dependent_type_location(cursor).line
        return cursor.location.line

    def get_ast_node_column(self, cursor):
        if cursor.type.kind == clang.cindex.TypeKind.DEPENDENT:
            return ClangParser.__extract_dependent_type_location(cursor).column
        return cursor.location.column

    def map_source_location_to_cursor(self, filename, line, column):
        logging.info("Mapping source location to type for " + str(filename))

        if filename not in self.tunits:
            return ''

        cursor = clang.cindex.Cursor.from_location(
                    self.tunits[filename],
                    clang.cindex.SourceLocation.from_position(
                        self.tunits[filename],
                        clang.cindex.File.from_name(self.tunits[filename], self.tunits[filename].spelling),
                        line,
                        column
                    )
                 )
        return cursor

    def get_definition(self, filename, line, column):
        if filename not in self.tunits:
            return None

        cursor = clang.cindex.Cursor.from_location(
                    self.tunits[filename],
                    clang.cindex.SourceLocation.from_position(
                        self.tunits[filename],
                        clang.cindex.File.from_name(self.tunits[filename], self.tunits[filename].spelling),
                        line,
                        column
                    )
                 )
        return cursor.get_definition()

    def find_all_references(self, filename, line, column):
        # TODO Use clang_findReferencesInFile() implementation?
        # TODO Why does the memory consumption grow when searching over many TU's?
        #       * Looks like as if there were no indexer results pre-loaded?
        #       * i.e. bigger project as cppcheck (.indexer contents is 1.4GB large)
        #
        # TODO Why doesn't the memory get freed after completion?
        #       * Memory allocator decides when it will swap the allocated
        #         memory back to the OS
        # TODO Second query (on different symbol?) does not increase RAM usage?!
        #       * Memory from previous step gets reused
        def visitor(ast_node, ast_parent_node, client_data):
            if (ast_node.location.file and ast_node.location.file.name == filename):  # we are not interested in symbols which got into this TU via includes
                node = ast_node.referenced if ast_node.referenced else ast_node
                if node.spelling == client_data.cursor.spelling:
                    if node.get_usr() == client_data.cursor.get_usr():
                        #
                        # At this point we can still get false positives. I.e.
                        #   * Multiple cursors with the same spelling and with the
                        #     same USR pointing to the same location.
                        #       * I.e. Parsing 'foobar()' will result in:
                        #           (1) CALL_EXPR with spelling set to 'foobar' and location set to location of 'foobar',
                        #           (2) UNEXPOSED_EXPR with spelling set to 'foobar' and location set to location of 'foobar',
                        #           (3) DECL_REF_EXPR with spelling set to 'foobar' and location set to location of 'foobar'
                        #         which will obviously give us 3 duplicates and right now I don't see any other easy way
                        #         eliminating those but to use set() with hashable SourceLocation's.
                        #
                        #   * Multiple cursors with the same spelling and with the
                        #     same USR pointing to different locations but not all
                        #     of them really being relevant.
                        #       * I.e. Parsing 's.foobar()' will result in:
                        #           (1) CALL_EXPR with spelling set to 'foobar' and location set to location of 's',
                        #           (2) MEMBER_REF_EXPR with spelling set to 'foobar' and location set to location of 'foobar',
                        #           (3) DECL_REF_EXPR with spelling set to 's' and location set to location of 's'
                        #       which will give us 2 duplicates, (1) & (2), and in this case we have to be able to identify
                        #       the ones which are not really the match. This we can do by tokenizing the cursor and trying to
                        #       see if token spelling at the given location still matches the spelling we are looking for.
                        #
                        for token in ast_node.get_tokens():
                            if ast_node.location == token.extent.start:
                                if node.spelling == token.spelling:
                                    client_data.references.add(ImmutableSourceLocation(ast_node.location))
                                break
            return ChildVisitResult.RECURSE.value

        if filename not in self.tunits:
            return []

        cursor = clang.cindex.Cursor.from_location(
                    self.tunits[filename],
                    clang.cindex.SourceLocation.from_position(
                        self.tunits[filename],
                        clang.cindex.File.from_name(self.tunits[filename], self.tunits[filename].spelling),
                        line,
                        column
                    )
                 )

        # TODO Optimize for:
        #           1. Local variables -> search within current TU
        #           2. Function parameters -> search within current TU
        #           3. Non-searchable items?
        references = set()
        client_data = collections.namedtuple('client_data', ['cursor', 'references'])
        for filename, tunit in self.tunits.iteritems():
            self.traverse(tunit.cursor, client_data(cursor.referenced if cursor.referenced else cursor, references), visitor)
        return references

    def save_to_disk(self, root_dir):
        try:
            logging.info('TUnits memory consumption (pympler) = ' + str(asizeof.asizeof(self.tunits)))
            for filename, tunit in self.tunits.iteritems():
                directory = os.path.dirname(os.path.join(root_dir, filename[1:len(filename)]))
                if not os.path.exists(directory):
                    os.makedirs(directory)
                logging.info('save_to_disk(): File = ' + filename)
                tunit.save(os.path.join(root_dir, filename[1:len(filename)] + '.ast'))
        except:
            logging.error(sys.exc_info()[0])
            return False
        return True

    def load_from_disk(self, root_dir):
        try:
            self.tunits.clear()
            for dirpath, dirs, files in os.walk(root_dir):
                for file in files:
                    name, extension = os.path.splitext(file)
                    if extension == '.ast':
                        parsing_result_filename = os.path.join(dirpath, file)
                        original_filename = parsing_result_filename[len(root_dir):-len('.ast')]
                        logging.info('load_from_disk(): File = ' + original_filename)
                        self.tunits[original_filename] = self.index.read(parsing_result_filename)
            logging.info('TUnits load_from_disk() memory consumption (pympler) = ' + str(asizeof.asizeof(self.tunits)))
        except:
            logging.error(sys.exc_info()[0])
            return False
        return True

    def drop_translation_unit(self, filename):
        if filename in self.tunits:
            del self.tunits[filename]

    def drop_all_translation_units(self):
        self.tunits.clear()

    def dump_tokens(self, cursor):
        for token in cursor.get_tokens():
            logging.debug(
                '%-22s' % ('[' + str(token.extent.start.line) + ', ' + str(token.extent.start.column) + ']:[' + str(token.extent.end.line) + ', ' + str(token.extent.end.column) + ']') +
                '%-30s' % token.spelling +
                '%-40s' % str(token.kind) +
                '%-40s' % str(token.cursor.kind) +
                'Token.Cursor.Extent %-25s' % ('[' + str(token.cursor.extent.start.line) + ', ' + str(token.cursor.extent.start.column) + ']:[' + str(token.cursor.extent.end.line) + ', ' + str(token.cursor.extent.end.column) + ']') +
                'Cursor.Extent %-25s' % ('[' + str(cursor.extent.start.line) + ', ' + str(cursor.extent.start.column) + ']:[' + str(cursor.extent.end.line) + ', ' + str(cursor.extent.end.column) + ']'))

    def dump_ast_nodes(self, filename):
        def visitor(ast_node, ast_parent_node, client_data):
            if ast_node.location.file and ast_node.location.file.name == filename:  # we're only interested in symbols from given file
                # if ast_node.kind in [clang.cindex.CursorKind.CALL_EXPR, clang.cindex.CursorKind.MEMBER_REF_EXPR]:
                #    self.dump_tokens(ast_node)

                logging.debug(
                    '%-12s' % ('[' + str(ast_node.location.line) + ', ' + str(ast_node.location.column) + ']') +
                    '%-40s' % str(ast_node.spelling) +
                    '%-40s' % str(ast_node.kind) +
                    '%-40s' % str(ast_node.type.spelling) +
                    '%-40s' % str(ast_node.type.kind) +
                    ('%-25s' % ('[' + str(ast_node.type.get_declaration().location.line) + ', ' + str(ast_node.type.get_declaration().location.column) + ']') if (ast_node.type and ast_node.type.get_declaration()) else '%-25s' % '-') +
                    ('%-25s' % ('[' + str(ast_node.get_definition().location.line) + ', ' + str(ast_node.get_definition().location.column) + ']') if (ast_node.get_definition()) else '%-25s' % '-') +
                    '%-40s' % str(ast_node.get_usr()) +
                    ('%-40s' % str(ClangParser.__get_overloaded_decl(ast_node, 0).spelling) if (ast_node.kind ==
                        clang.cindex.CursorKind.OVERLOADED_DECL_REF and ClangParser.__get_num_overloaded_decls(ast_node)) else '%-40s' % '-') +
                    ('%-40s' % str(ClangParser.__get_overloaded_decl(ast_node, 0).kind) if (ast_node.kind ==
                        clang.cindex.CursorKind.OVERLOADED_DECL_REF and ClangParser.__get_num_overloaded_decls(ast_node)) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.spelling) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.kind) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.type.spelling) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.type.kind) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.result_type.spelling) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.result_type.kind) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.canonical.spelling) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.canonical.kind) if (ast_node.referenced) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.semantic_parent.spelling) if (ast_node.referenced and ast_node.referenced.semantic_parent) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.semantic_parent.kind) if (ast_node.referenced and ast_node.referenced.semantic_parent) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.lexical_parent.spelling) if (ast_node.referenced and ast_node.referenced.lexical_parent) else '%-40s' % '-') +
                    ('%-40s' % str(ast_node.referenced.lexical_parent.kind) if (ast_node.referenced and ast_node.referenced.lexical_parent) else '%-40s' % '-') +
                    ('%-25s' % ('[' + str(ast_node.referenced.type.get_declaration().location.line) + ', ' + str(ast_node.referenced.type.get_declaration().location.column) + ']')
                        if (ast_node.referenced and ast_node.referenced.type and ast_node.referenced.type.get_declaration()) else '%-25s' % '-') +
                    ('%-25s' % ('[' + str(ast_node.referenced.get_definition().location.line) + ', ' + str(ast_node.referenced.get_definition().location.column) + ']')
                        if (ast_node.referenced and ast_node.referenced.get_definition()) else '%-25s' % '-') +
                    ('%-40s' % str(ast_node.referenced.get_usr()) if ast_node.referenced else '%-40s' % '-')
                )

            return ChildVisitResult.RECURSE.value


        if filename in self.tunits:
            logging.debug(
                '%-12s' % '[Line, Col]' +
                '%-40s' % 'Spelling' +
                '%-40s' % 'Kind' +
                '%-40s' % 'Type.Spelling' +
                '%-40s' % 'Type.Kind' +
                '%-25s' % 'Declaration.Location' +
                '%-25s' % 'Definition.Location' +
                '%-40s' % 'USR' +
                '%-40s' % 'OverloadedDecl' + '%-40s' % 'NumOverloadedDecls' +
                '%-40s' % 'Referenced.Spelling' + '%-40s' % 'Referenced.Kind' +
                '%-40s' % 'Referenced.Type.Spelling' + '%-40s' % 'Referenced.Type.Kind' +
                '%-40s' % 'Referenced.ResultType.Spelling' + '%-40s' % 'Referenced.ResultType.Kind' +
                '%-40s' % 'Referenced.Canonical.Spelling' + '%-40s' % 'Referenced.Canonical.Kind' +
                '%-40s' % 'Referenced.SemanticParent.Spelling' + '%-40s' % 'Referenced.SemanticParent.Kind' +
                '%-40s' % 'Referenced.LexicalParent.Spelling' + '%-40s' % 'Referenced.LexicalParent.Kind' +
                '%-25s' % 'Referenced.Declaration.Location' +
                '%-25s' % 'Referenced.Definition.Location' +
                '%-25s' % 'Referenced.USR'
            )

            logging.debug('----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------')
            self.traverse(self.tunits[filename].cursor, None, visitor)

    @staticmethod
    def __extract_dependent_type_kind(cursor):
        # For cursors whose CursorKind is MEMBER_REF_EXPR and whose TypeKind is DEPENDENT we don't get much information
        # from libclang API directly (i.e. cursor spelling will be empty).
        # Instead, we can extract such information indirectly by:
        #   1. Tokenizing the cursor
        #       * It will contain all the tokens that make up the MEMBER_REF_EXPR and therefore all the spellings, locations, extents, etc.
        #   2. Finding a token whose:
        #       * TokenKind is IDENTIFIER
        #       * CursorKind of a cursor that it corresponds to matches the MEMBER_REF_EXPR
        #       * Extent of a cursor that it corresponds to matches the extent of original cursor
        #   3. If CursorKind of original cursor AST parent is CALL_EXPR then we know that token found is CursorKind.CXX_METHOD
        #      If CursorKind of original cursor AST parent is not CALL_EXPR then we know that token found is CursorKind.FIELD_DECL
        assert cursor.type.kind == clang.cindex.TypeKind.DEPENDENT
        if cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR:
            if cursor.ast_parent and (cursor.ast_parent.kind == clang.cindex.CursorKind.CALL_EXPR):
                for token in cursor.get_tokens():
                    if (token.kind == clang.cindex.TokenKind.IDENTIFIER) and (token.cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR) and (token.cursor.extent == cursor.extent):
                        return clang.cindex.CursorKind.CXX_METHOD # We've got a function member call
            else:
                for token in cursor.get_tokens():
                    if (token.kind == clang.cindex.TokenKind.IDENTIFIER) and (token.cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR) and (token.cursor.extent == cursor.extent):
                        return clang.cindex.CursorKind.FIELD_DECL # We've got a data member
        return cursor.kind

    @staticmethod
    def __extract_dependent_type_spelling(cursor):
        # See __extract_dependent_type_kind() for more details but in essence we have to tokenize the cursor and
        # return the spelling of appropriate token.
        assert cursor.type.kind == clang.cindex.TypeKind.DEPENDENT
        if cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR:
            for token in cursor.get_tokens():
                if (token.kind == clang.cindex.TokenKind.IDENTIFIER) and (token.cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR) and (token.cursor.extent == cursor.extent):
                    return token.spelling
        return cursor.spelling

    @staticmethod
    def __extract_dependent_type_location(cursor):
        # See __extract_dependent_type_kind() for more details but in essence we have to tokenize the cursor and
        # return the location of appropriate token.
        assert cursor.type.kind == clang.cindex.TypeKind.DEPENDENT
        if cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR:
            for token in cursor.get_tokens():
                if (token.kind == clang.cindex.TokenKind.IDENTIFIER) and (token.cursor.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR) and (token.cursor.extent == cursor.extent):
                    return token.location
        return cursor.location

    @staticmethod
    def to_ast_node_id(kind):
        if (kind == clang.cindex.CursorKind.NAMESPACE):
            return ASTNodeId.getNamespaceId()
        if (kind in [clang.cindex.CursorKind.CLASS_DECL, clang.cindex.CursorKind.CLASS_TEMPLATE, clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION]):
            return ASTNodeId.getClassId()
        if (kind == clang.cindex.CursorKind.STRUCT_DECL):
            return ASTNodeId.getStructId()
        if (kind == clang.cindex.CursorKind.ENUM_DECL):
            return ASTNodeId.getEnumId()
        if (kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL):
            return ASTNodeId.getEnumValueId()
        if (kind == clang.cindex.CursorKind.UNION_DECL):
            return ASTNodeId.getUnionId()
        if (kind == clang.cindex.CursorKind.FIELD_DECL):
            return ASTNodeId.getFieldId()
        if (kind == clang.cindex.CursorKind.VAR_DECL):
            return ASTNodeId.getLocalVariableId()
        if (kind in [clang.cindex.CursorKind.FUNCTION_DECL, clang.cindex.CursorKind.FUNCTION_TEMPLATE]):
            return ASTNodeId.getFunctionId()
        if (kind in [clang.cindex.CursorKind.CXX_METHOD, clang.cindex.CursorKind.CONSTRUCTOR, clang.cindex.CursorKind.DESTRUCTOR]):
            return ASTNodeId.getMethodId()
        if (kind == clang.cindex.CursorKind.PARM_DECL):
            return ASTNodeId.getFunctionParameterId()
        if (kind == clang.cindex.CursorKind.TEMPLATE_TYPE_PARAMETER):
            return ASTNodeId.getTemplateTypeParameterId()
        if (kind == clang.cindex.CursorKind.TEMPLATE_NON_TYPE_PARAMETER):
            return ASTNodeId.getTemplateNonTypeParameterId()
        if (kind == clang.cindex.CursorKind.TEMPLATE_TEMPLATE_PARAMETER):
            return ASTNodeId.getTemplateTemplateParameterId()
        if (kind == clang.cindex.CursorKind.MACRO_DEFINITION):
            return ASTNodeId.getMacroDefinitionId()
        if (kind == clang.cindex.CursorKind.MACRO_INSTANTIATION):
            return ASTNodeId.getMacroInstantiationId()
        if (kind in [clang.cindex.CursorKind.TYPEDEF_DECL, clang.cindex.CursorKind.TYPE_ALIAS_DECL]):
            return ASTNodeId.getTypedefId()
        if (kind == clang.cindex.CursorKind.NAMESPACE_ALIAS):
            return ASTNodeId.getNamespaceAliasId()
        if (kind == clang.cindex.CursorKind.USING_DIRECTIVE):
            return ASTNodeId.getUsingDirectiveId()
        if (kind == clang.cindex.CursorKind.USING_DECLARATION):
            return ASTNodeId.getUsingDeclarationId()
        return ASTNodeId.getUnsupportedId()

    # TODO Shall be removed once 'cindex.py' exposes it in its interface.
    @staticmethod
    def __get_num_overloaded_decls(cursor):
        return clang.cindex.conf.lib.clang_getNumOverloadedDecls(cursor)

    # TODO Shall be removed once 'cindex.py' exposes it in its interface.
    @staticmethod
    def __get_overloaded_decl(cursor, num):
        return clang.cindex.conf.lib.clang_getOverloadedDecl(cursor, num)
